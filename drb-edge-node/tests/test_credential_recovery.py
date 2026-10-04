"""
node-26#14 — a node with no key, or a stale key, recovers on its own:
re-read credentials.json, else fetch over HTTPS with the pickup secret, then
rebuild the MQTT client in place. node-26#15 — node_update hands off to the
host updater via a sentinel file.
"""
import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from app.config import settings
from app.internal import credentials, key_pickup, mqtt_manager as mm_mod, update_request
from app.internal.mqtt_manager import MQTTManager, _is_auth_failure


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(credentials, "_CREDS_FILE", tmp_path / "credentials.json")
    monkeypatch.setattr(credentials, "_PICKUP_FILE", tmp_path / "pickup_secret")
    monkeypatch.setattr(credentials, "_api_key", None)
    monkeypatch.setattr(settings, "config_path", str(tmp_path))
    monkeypatch.setattr(settings, "c2_url", "https://c2.example")
    monkeypatch.setattr(settings, "mqtt_user", None)
    monkeypatch.setattr(mm_mod, "RECOVERY_MIN_DELAY", 0)
    yield tmp_path


def _manager(client_key=None):
    m = MQTTManager()
    m._client_key = client_key
    m._rebuild_client = AsyncMock()
    return m


# --- credentials.py -----------------------------------------------------------

def test_reload_picks_up_a_key_written_after_startup(isolated):
    credentials.load()
    assert credentials.get_api_key() is None
    data = json.loads((isolated / "credentials.json").read_text())
    data["api_key"] = "written-by-install-sh"
    (isolated / "credentials.json").write_text(json.dumps(data))
    assert credentials.reload_api_key() == "written-by-install-sh"


def test_write_is_atomic_and_private(isolated):
    credentials.save_api_key("k1")
    f = isolated / "credentials.json"
    assert json.loads(f.read_text())["api_key"] == "k1"
    assert (f.stat().st_mode & 0o777) == 0o600
    assert not (isolated / "credentials.json.tmp").exists()


def test_pickup_secret_read_and_absent(isolated):
    assert credentials.get_pickup_secret() is None
    (isolated / "pickup_secret").write_text("abc123\n")
    assert credentials.get_pickup_secret() == "abc123"


# --- auth-failure classification -------------------------------------------------

@pytest.mark.parametrize("code", [4, 5, 134, 135])
def test_auth_failure_codes(code):
    assert _is_auth_failure(code)


def test_non_auth_failures_do_not_trigger_recovery():
    assert not _is_auth_failure(3)  # server unavailable
    assert not _is_auth_failure(136)


def test_paho_reason_code_objects_classified():
    from paho.mqtt.reasoncodes import ReasonCode
    from paho.mqtt.packettypes import PacketTypes
    assert _is_auth_failure(ReasonCode(PacketTypes.CONNACK, "Not authorized"))
    assert _is_auth_failure(ReasonCode(PacketTypes.CONNACK, "Bad user name or password"))
    assert not _is_auth_failure(ReasonCode(PacketTypes.CONNACK, "Server unavailable"))


# --- recovery loop ----------------------------------------------------------------

async def test_recovery_uses_key_already_on_disk_without_calling_c2(isolated, monkeypatch):
    credentials.save_api_key("disk-key")
    monkeypatch.setattr(credentials, "_api_key", None)  # process started before the write
    fetch = AsyncMock()
    monkeypatch.setattr(key_pickup, "fetch_api_key", fetch)
    m = _manager(client_key=None)
    await m._recover_credentials()
    m._rebuild_client.assert_awaited_once()
    fetch.assert_not_awaited()


async def test_recovery_fetches_over_https_and_persists(isolated, monkeypatch):
    monkeypatch.setattr(key_pickup, "fetch_api_key", AsyncMock(return_value="fresh-key"))
    m = _manager(client_key="stale-key")
    monkeypatch.setattr(credentials, "_api_key", "stale-key")
    await m._recover_credentials()
    m._rebuild_client.assert_awaited_once()
    assert credentials.get_api_key() == "fresh-key"
    assert json.loads((isolated / "credentials.json").read_text())["api_key"] == "fresh-key"


async def test_recovery_backs_off_until_approved(isolated, monkeypatch):
    fetch = AsyncMock(side_effect=[None, None, "approved-key"])
    monkeypatch.setattr(key_pickup, "fetch_api_key", fetch)
    m = _manager()
    await asyncio.wait_for(m._recover_credentials(), timeout=5)
    assert fetch.await_count == 3
    m._rebuild_client.assert_awaited_once()


async def test_recovery_stops_if_connection_recovers_meanwhile(isolated, monkeypatch):
    monkeypatch.setattr(key_pickup, "fetch_api_key", AsyncMock(return_value=None))
    m = _manager()
    m._connected = True
    await asyncio.wait_for(m._recover_credentials(), timeout=5)
    m._rebuild_client.assert_not_awaited()


async def test_start_recovery_is_single_flight(isolated, monkeypatch):
    gate = asyncio.Event()

    async def slow_fetch():
        await gate.wait()
        return "k"
    monkeypatch.setattr(key_pickup, "fetch_api_key", slow_fetch)
    m = _manager()
    m._start_recovery("a")
    first = m._recovery_task
    m._start_recovery("b")
    assert m._recovery_task is first
    gate.set()
    await first


async def test_connect_without_key_starts_recovery(isolated, monkeypatch):
    m = MQTTManager()
    started = []
    monkeypatch.setattr(m, "_connect_with_retry", AsyncMock())
    monkeypatch.setattr(m, "_start_recovery", lambda why: started.append(why))
    await m.connect()
    assert started == ["no API key at startup"]


def test_update_api_key_changes_creds_for_next_reconnect(isolated):
    m = MQTTManager()
    m._client = m._build_client()
    m.update_api_key("rotated")
    assert m._client._password == b"rotated"
    assert m._client_key == "rotated"


# --- key_pickup.fetch_api_key -----------------------------------------------------

def _mock_http(monkeypatch, handler):
    real = httpx.AsyncClient

    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real(*a, **kw)
    monkeypatch.setattr(key_pickup.httpx, "AsyncClient", factory)


async def test_fetch_sends_pickup_secret_and_returns_key(isolated, monkeypatch):
    (isolated / "pickup_secret").write_text("the-secret")
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["secret"] = request.headers.get("X-Pickup-Secret")
        return httpx.Response(200, json={"approval_status": "approved", "api_key": "K"})
    _mock_http(monkeypatch, handler)
    assert await key_pickup.fetch_api_key() == "K"
    assert seen == {"url": f"https://c2.example/nodes/{settings.node_id}/credentials", "secret": "the-secret"}


@pytest.mark.parametrize("resp", [
    httpx.Response(200, json={"approval_status": "pending", "api_key": None}),
    httpx.Response(401, json={"detail": "Invalid pickup secret"}),
    httpx.Response(404),
    httpx.Response(500),
    httpx.Response(200, text="not json"),
])
async def test_fetch_returns_none_when_no_key(isolated, monkeypatch, resp):
    (isolated / "pickup_secret").write_text("s")
    _mock_http(monkeypatch, lambda request: resp)
    assert await key_pickup.fetch_api_key() is None


async def test_fetch_without_pickup_secret_does_not_call_c2(isolated, monkeypatch):
    def handler(request):
        raise AssertionError("must not call C2 without a pickup secret")
    _mock_http(monkeypatch, handler)
    assert await key_pickup.fetch_api_key() is None


# --- node_update -> host sentinel ---------------------------------------------------

def test_node_update_writes_sentinel(isolated):
    assert update_request.request_host_update() is True
    data = json.loads((isolated / "update-requested").read_text())
    assert "requested_at" in data
