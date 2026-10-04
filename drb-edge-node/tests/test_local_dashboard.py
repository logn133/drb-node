"""
Local dashboard: the health checklist (/api/health) and choosing what to
listen to without C2 (/api/listen, /api/config/system, /api/config/revert).

Same standalone-app pattern as test_auth_endpoints.py — the real routers,
none of app.main's lifespan. OP25/MQTT are mocked at the client boundary.
"""
import base64
import time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.internal import config_manager, credentials, health
from app.internal.op25_client import parse_terminal_messages
from app.routers import api, ui


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(credentials, "_CREDS_FILE", tmp_path / "credentials.json")
    monkeypatch.setattr(credentials, "_api_key", None)
    monkeypatch.setattr(credentials, "_auth_salt", None)
    monkeypatch.setattr(credentials, "_session_secret", None)
    monkeypatch.setattr(config_manager, "_CONFIG_FILE", tmp_path / "node_config.json")
    monkeypatch.setattr(settings, "dashboard_username", "tester")
    monkeypatch.setattr(settings, "dashboard_password", "s3cret-pass")
    yield


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(api.router)
    app.include_router(ui.router)
    auth = base64.b64encode(b"tester:s3cret-pass").decode()
    with TestClient(app, headers={"Authorization": f"Basic {auth}"}) as c:
        yield c


@pytest.fixture
def op25():
    """Mock the OP25 side of an apply: generate, stop, start, checkin."""
    with patch("app.main._generate_op25_config", AsyncMock(return_value=True)) as gen, \
         patch("app.routers.api.op25_client.stop", AsyncMock(return_value=True)) as stop, \
         patch("app.routers.api.op25_client.start_with_reason", AsyncMock(return_value=None)) as start, \
         patch("app.routers.api.asyncio.sleep", AsyncMock()), \
         patch("app.routers.api.mqtt_manager.publish_checkin", AsyncMock()) as checkin:
        yield {"gen": gen, "stop": stop, "start": start, "checkin": checkin}


# ---------------------------------------------------------------------------
# Health checklist — pure function of facts
# ---------------------------------------------------------------------------

def _facts(**over):
    now = 1_000_000.0
    f = {
        "now": now, "node_id": "toughny", "default_password": False,
        "c2_url": "https://api.example", "has_api_key": True, "mqtt_connected": True,
        "mqtt_broker": "mqtt.example", "mqtt_port": 8883, "sdr_count": 1,
        "source": {"name": "County", "type": "P25", "system_id": "s1", "local": False},
        "op25_status": "running",
        "console": {"last_poll_epoch": now - 1, "channels": [], "trunk": [{"last_tsbk": now - 1, "rxchan": 851012500}],
                    "last_heard": None},
        "audio_capturing": True, "last_voice_epoch": now - 30, "icecast_live": True, "icecast_port": 8000,
    }
    f.update(over)
    return f


def _by_id(checks):
    return {c["id"]: c for c in checks}


def test_healthy_node_is_all_ok():
    checks = health.build_checks(_facts())
    assert all(c["state"] == "ok" for c in checks), [c for c in checks if c["state"] != "ok"]
    assert "851.0125 MHz" in _by_id(checks)["decode"]["summary"]


def test_unapproved_node_explains_wait_and_mqtt_refusal():
    """Toughny's state: C2 set, no API key, broker refuses, nothing chosen."""
    c = _by_id(health.build_checks(_facts(has_api_key=False, mqtt_connected=False, source=None,
                                          op25_status="stopped")))
    assert c["c2"]["state"] == "warn" and "approve" in c["c2"]["action"]
    assert "toughny" in c["c2"]["action"]
    assert c["mqtt"]["state"] == "warn"
    assert c["source"]["state"] == "warn" and "Listen locally" in c["source"]["action"]
    assert c["op25"]["state"] == "off"
    assert c["decode"]["state"] == "off"


def test_key_on_disk_but_mqtt_down_says_restart():
    c = _by_id(health.build_checks(_facts(mqtt_connected=False)))
    assert c["mqtt"]["state"] == "error"
    assert "restart the edge-node container" in c["mqtt"]["action"]


def test_standalone_node_is_not_an_error():
    c = _by_id(health.build_checks(_facts(c2_url=None, has_api_key=False, mqtt_connected=False)))
    assert c["c2"]["state"] == "off"
    assert c["mqtt"]["state"] == "off"


def test_no_sdr_and_crashed_op25():
    c = _by_id(health.build_checks(_facts(sdr_count=0, op25_status="crashed")))
    assert c["sdr"]["state"] == "error"
    assert c["op25"]["state"] == "error" and "log" in c["op25"]["action"]


def test_running_but_no_control_channel():
    now = 1_000_000.0
    c = _by_id(health.build_checks(_facts(console={"last_poll_epoch": now, "channels": [],
                                                   "trunk": [{"last_tsbk": 0}], "last_heard": None})))
    assert c["decode"]["state"] == "error"


def test_analog_needs_no_control_channel():
    now = 1_000_000.0
    c = _by_id(health.build_checks(_facts(
        source={"name": "Fire", "type": "NBFM", "system_id": "local-analog", "local": True},
        console={"last_poll_epoch": now, "channels": [{"freq": 154430000}], "trunk": [], "last_heard": None},
    )))
    assert c["decode"]["state"] == "ok" and "154.4300 MHz" in c["decode"]["summary"]
    # node-26#20: analog calls are recorded now (single-channel attribution).
    assert c["decode"]["action"] == ""
    assert "this dashboard" in c["source"]["summary"]


_CONV_SRC = {"name": "White Plains", "type": "P25", "mode": "conventional", "system_id": "wp", "local": False}
_PD1 = {"name": "PD1 Dispatch", "freq": 460400000, "nac": None, "encrypted": False, "algid": None, "keyid": None}
_PD3 = {"name": "PD3 Tac", "freq": 460425000, "nac": None, "encrypted": False, "algid": None, "keyid": None}


def _conv_facts(chans):
    now = 1_000_000.0
    return _facts(source=_CONV_SRC, conventional=chans,
                  console={"last_poll_epoch": now, "channels": [], "trunk": [], "last_heard": None})


def test_conventional_quiet_channels_are_ok_not_a_control_channel_error():
    c = _by_id(health.build_checks(_conv_facts([_PD1, _PD3])))
    assert c["decode"]["state"] == "ok"
    assert "PD1 Dispatch (460.4000 MHz)" in c["decode"]["summary"]
    assert "control channel" not in c["decode"]["summary"]
    assert "P25 conventional" in c["source"]["summary"]


def test_conventional_decoding_shows_nac():
    c = _by_id(health.build_checks(_conv_facts([dict(_PD1, nac=0x293), _PD3])))
    assert c["decode"]["state"] == "ok" and "NAC 0x293" in c["decode"]["summary"]


def test_conventional_all_encrypted_is_an_error_naming_the_algorithm():
    enc = [dict(_PD1, encrypted=True, algid=0x84, keyid=0x1234), dict(_PD3, encrypted=True, algid=0x84)]
    c = _by_id(health.build_checks(_conv_facts(enc)))
    assert c["decode"]["state"] == "error"
    assert "AES-256" in c["decode"]["summary"] and "0x1234" in c["decode"]["summary"]
    assert "different system" in c["decode"]["action"]


def test_conventional_partly_encrypted_warns():
    c = _by_id(health.build_checks(_conv_facts([dict(_PD1, encrypted=True, algid=0x81), _PD3])))
    assert c["decode"]["state"] == "warn" and "PD1 Dispatch" in c["decode"]["summary"]


def test_op25_unreachable_and_icecast_down():
    c = _by_id(health.build_checks(_facts(op25_status=None, icecast_live=None)))
    assert c["op25"]["state"] == "error"
    assert c["stream"]["state"] == "error"


def test_default_password_warns():
    assert _by_id(health.build_checks(_facts(default_password=True)))["password"]["state"] == "warn"


def test_trunk_update_is_parsed():
    update = parse_terminal_messages([
        {"json_type": "trunk_update", "0": {"last_tsbk": 5.0, "rxchan": 851012500}, "nac": 0},
        {"json_type": "channel_update", "channels": ["0"], "0": {"freq": 1, "tgid": None}},
    ])
    assert update.trunk == [{"last_tsbk": 5.0, "rxchan": 851012500}]
    assert len(update.channels) == 1


def test_health_route_returns_checks(client):
    facts = _facts(now=time.time())
    with patch("app.internal.health.gather_facts", AsyncMock(return_value=facts)):
        r = client.get("/api/health")
    assert r.status_code == 200
    assert [c["id"] for c in r.json()["checks"]][:3] == ["password", "c2", "mqtt"]


# ---------------------------------------------------------------------------
# Choosing what to listen to
# ---------------------------------------------------------------------------

def test_listen_p25_saves_override_and_restarts_op25(client, op25):
    r = client.post("/api/listen", json={
        "mode": "p25", "frequencies": [851.0125, 851.5125], "talkgroups": [{"id": 1001, "name": "Fire"}],
    })
    assert r.status_code == 200, r.text
    cfg = config_manager.load_node_config()
    assert cfg.override_config.type == "P25"
    assert cfg.override_config.config["control_channels"] == [851.0125, 851.5125]
    assert cfg.override_config.config["talkgroups"] == [{"id": 1001, "name": "Fire"}]
    op25["gen"].assert_awaited_once()
    op25["start"].assert_awaited_once()
    op25["checkin"].assert_not_awaited()  # MQTT not connected in tests


def test_listen_analog_single_frequency(client, op25):
    r = client.post("/api/listen", json={"mode": "analog", "frequencies": [154.43], "name": "Fire"})
    assert r.status_code == 200, r.text
    cfg = config_manager.load_node_config().override_config
    assert (cfg.type, cfg.name, cfg.config["control_channels"]) == ("NBFM", "Fire", [154.43])


def test_listen_p25_conventional(client, op25):
    r = client.post("/api/listen", json={
        "mode": "p25conv", "frequencies": [460.4, 460.425], "name": "White Plains", "nac": "0x293",
    })
    assert r.status_code == 200, r.text
    cfg = config_manager.load_node_config().override_config
    assert cfg.type == "P25" and cfg.config["mode"] == "conventional"
    assert "control_channels" not in cfg.config
    assert [c["freq"] for c in cfg.config["channels"]] == [460.4, 460.425]
    assert {c["nac"] for c in cfg.config["channels"]} == {"293"}
    op25["gen"].assert_awaited_once()
    status = client.get("/api/status").json()
    assert status["system_kind"] == "P25 conventional"


@pytest.mark.parametrize("body,needle", [
    ({"mode": "p25conv", "frequencies": [460.4, 460.425, 460.45]}, "at most 2"),
    ({"mode": "p25conv", "frequencies": [460.4, 462.6]}, "apart"),
    ({"mode": "p25conv", "frequencies": [460.4, 460.4]}, "twice"),
    ({"mode": "p25conv", "frequencies": [460.4], "nac": "zz"}, "hex"),
    ({"mode": "p25", "frequencies": []}, "at least one"),
    ({"mode": "p25", "frequencies": [851012500]}, "MHz"),
    ({"mode": "analog", "frequencies": [154.43, 155.1]}, "exactly one"),
    ({"mode": "system", "system_id": "nope"}, "saved list"),
])
def test_listen_rejects_bad_input_with_a_readable_reason(client, op25, body, needle):
    r = client.post("/api/listen", json=body)
    assert r.status_code in (400, 404)
    assert needle in r.json()["detail"]
    op25["gen"].assert_not_awaited()


def test_listen_reports_op25_start_failure(client, op25):
    op25["start"].return_value = "No OP25 config yet — choose a system or frequency first."
    r = client.post("/api/listen", json={"mode": "analog", "frequencies": [154.43]})
    assert r.status_code == 502
    assert "did not start" in r.json()["detail"]


def test_dmr_is_refused_up_front(client, op25):
    r = client.post("/api/config/override", json={"system_config": {
        "system_id": "x", "name": "x", "type": "DMR", "config": {"control_channels": [450.1]},
    }})
    assert r.status_code == 400 and "DMR" in r.json()["detail"]
    op25["gen"].assert_not_awaited()


def test_config_system_goes_through_op25_generation(client, op25, tmp_path, monkeypatch):
    """It used to dump the raw blob into active.cfg.json — not an OP25 config."""
    monkeypatch.setattr(settings, "config_path", str(tmp_path))
    r = client.post("/api/config/system", json={
        "system_id": "s1", "name": "County", "type": "P25", "config": {"control_channels": [851.0125]},
    })
    assert r.status_code == 200, r.text
    op25["gen"].assert_awaited_once()
    assert not (tmp_path / "active.cfg.json").exists()
    cfg = config_manager.load_node_config()
    assert cfg.configured and cfg.system_config.name == "County"


def test_revert_without_c2_system_stops_op25(client, op25):
    client.post("/api/listen", json={"mode": "analog", "frequencies": [154.43]})
    op25["stop"].reset_mock()
    r = client.post("/api/config/revert")
    assert r.status_code == 200
    assert config_manager.load_node_config().override_config is None
    op25["stop"].assert_awaited_once()


def test_status_exposes_stream_parts_and_source(client, op25):
    client.post("/api/listen", json={"mode": "analog", "frequencies": [154.43]})
    with patch("app.routers.api.op25_client.status", AsyncMock(return_value={"status": "running"})):
        d = client.get("/api/status").json()
    assert d["icecast_port"] == settings.icecast_port and d["icecast_mount"] == settings.icecast_mount
    assert d["local_choice"] is True and d["has_c2_system"] is False
    assert d["system_type"] == "NBFM"


def test_dashboard_page_has_no_dead_api_instruction(client):
    client.post("/login", data={"username": "tester", "password": "s3cret-pass"}, follow_redirects=False)
    html = client.get("/").text
    assert "POST /api/config/system" not in html
    assert "/api/listen" in html and "/api/health" in html
