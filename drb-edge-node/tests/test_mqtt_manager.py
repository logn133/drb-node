"""
Unit tests for mqtt_manager's per-node auth + TLS wiring
(MQTT-PUBLIC-AUTH-PLAN.md dynsec cutover).

Pure client-construction tests — _build_client() only builds a paho Client
object, it never calls .connect(), so no real broker is involved. What's
verified here is the credential/TLS *selection logic*, matching what the
server's dynsec plugin now expects (username=node_id, password=api_key,
default-verified TLS on the public listener) — see
Server/drb-c2-core/app/internal/dynsec.py and mosquitto.conf (read-only
reference, not touched by this change).
"""
import ssl
from unittest.mock import patch

import pytest

from app.config import settings
from app.internal import credentials
from app.internal.mqtt_manager import mqtt_manager


@pytest.fixture(autouse=True)
def isolated_mqtt_settings(monkeypatch):
    """Every test gets known, isolated mqtt_* settings and a clean
    credentials._api_key so tests can't see real .env values or leak state
    between tests (mirrors the isolated_credentials fixture in test_auth.py)."""
    monkeypatch.setattr(settings, "mqtt_user", None)
    monkeypatch.setattr(settings, "mqtt_pass", None)
    monkeypatch.setattr(settings, "mqtt_tls", False)
    monkeypatch.setattr(credentials, "_api_key", None)
    yield


# ---------------------------------------------------------------------------
# Credential selection: api_key > legacy mqtt_user > anonymous
# ---------------------------------------------------------------------------

def test_build_client_uses_node_id_and_api_key_when_present(monkeypatch):
    monkeypatch.setattr(credentials, "_api_key", "the-api-key")

    client = mqtt_manager._build_client()

    assert client._username == settings.node_id.encode()
    assert client._password == b"the-api-key"


def test_build_client_falls_back_to_legacy_mqtt_user_without_api_key(monkeypatch):
    monkeypatch.setattr(settings, "mqtt_user", "drb-node")
    monkeypatch.setattr(settings, "mqtt_pass", "legacy-pass")

    client = mqtt_manager._build_client()

    assert client._username == b"drb-node"
    assert client._password == b"legacy-pass"


def test_build_client_api_key_takes_priority_over_legacy_mqtt_user(monkeypatch):
    """Once a node has a real api_key, it must never fall back to the shared
    legacy login even if MQTT_USER/MQTT_PASS are still set in .env."""
    monkeypatch.setattr(credentials, "_api_key", "the-api-key")
    monkeypatch.setattr(settings, "mqtt_user", "drb-node")
    monkeypatch.setattr(settings, "mqtt_pass", "legacy-pass")

    client = mqtt_manager._build_client()

    assert client._username == settings.node_id.encode()
    assert client._password == b"the-api-key"


def test_build_client_with_no_credentials_connects_anonymously(monkeypatch):
    """No api_key on disk, no legacy login configured: _build_client() must
    still return a usable client (paho, not this code, decides what happens
    on the wire — the dynsec broker refuses it, see the warning test below).
    This must never raise."""
    client = mqtt_manager._build_client()

    assert client._username is None
    assert client._password is None


def test_build_client_warns_when_no_credentials_available(caplog):
    with caplog.at_level("WARNING", logger="drb-edge-node"):
        mqtt_manager._build_client()

    messages = [r.message for r in caplog.records]
    assert any("No API key" in m for m in messages), \
        "an unenrolled node must log a clear, greppable warning, not fail silently"


def test_build_client_does_not_warn_when_api_key_present(monkeypatch, caplog):
    monkeypatch.setattr(credentials, "_api_key", "the-api-key")

    with caplog.at_level("WARNING", logger="drb-edge-node"):
        mqtt_manager._build_client()

    assert not any("No API key" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# TLS
# ---------------------------------------------------------------------------

def test_build_client_no_tls_by_default(monkeypatch):
    monkeypatch.setattr(credentials, "_api_key", "the-api-key")
    monkeypatch.setattr(settings, "mqtt_tls", False)

    client = mqtt_manager._build_client()

    assert client._ssl_context is None


def test_build_client_enables_tls_with_default_verification(monkeypatch):
    monkeypatch.setattr(credentials, "_api_key", "the-api-key")
    monkeypatch.setattr(settings, "mqtt_tls", True)

    client = mqtt_manager._build_client()

    assert isinstance(client._ssl_context, ssl.SSLContext)
    # The whole point: default CA verification against the broker's real
    # Let's Encrypt cert must stay ON. tls_insecure_set(True) must never be
    # called — that would defeat verification entirely.
    assert client._ssl_context.verify_mode == ssl.CERT_REQUIRED
    assert client._tls_insecure is False


# ---------------------------------------------------------------------------
# Offline call buffer must be untouched by the auth/TLS change
# ---------------------------------------------------------------------------

def test_build_client_does_not_touch_offline_buffer(monkeypatch):
    """_build_client() is called fresh on every connect(); it must never
    reset or otherwise touch the offline call-buffer deque — that survives
    reconnects/auth changes by design (the whole point of the buffer)."""
    monkeypatch.setattr(credentials, "_api_key", "the-api-key")
    mqtt_manager._offline_buffer.append(("nodes/test/metadata", {"call_id": "sentinel"}))

    with patch.object(mqtt_manager, "_offline_buffer", mqtt_manager._offline_buffer):
        mqtt_manager._build_client()

    assert list(mqtt_manager._offline_buffer) == [("nodes/test/metadata", {"call_id": "sentinel"})]
    mqtt_manager._offline_buffer.clear()


def test_disk_report_reads_free_and_total(tmp_path):
    from app.internal.mqtt_manager import disk_report
    rep = disk_report(str(tmp_path))
    assert rep["disk_total_bytes"] > 0
    assert 0 <= rep["disk_free_bytes"] <= rep["disk_total_bytes"]


def test_disk_report_nulls_when_path_unreadable(tmp_path):
    # node-26#41: nulls (not omitted keys) so C2 never keeps a stale reading.
    from app.internal.mqtt_manager import disk_report
    assert disk_report(str(tmp_path / "missing")) == {"disk_free_bytes": None, "disk_total_bytes": None}
