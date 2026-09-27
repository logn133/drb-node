"""
node-26#9 / #11 — which SDR does what: secondary priority, per-service pins,
and OP25 always opening its dongle by serial.
"""
import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.models import NodeConfig, normalize_sdr_pins, normalize_secondary_priority

DEVS = [
    {"index": 0, "serial": "69420", "name": "RTL", "duplicate_serial": False},
    {"index": 1, "serial": "00000001", "name": "RTL", "duplicate_serial": False},
]


def _cfg(**kw) -> NodeConfig:
    return NodeConfig(node_id="n1", node_name="N1", lat=0.0, lon=0.0, **kw)


def test_normalize_priority_keeps_order_drops_unknown_and_duplicates():
    assert normalize_secondary_priority(["ais", "bogus", "adsb", "ais"]) == ["ais", "adsb"]


def test_legacy_single_mode_migrates_to_priority():
    assert _cfg(secondary_sdr_mode="adsb").secondary_sdr_priority == ["adsb"]
    assert _cfg(secondary_sdr_mode="adsb", secondary_sdr_priority=["ais"]).secondary_sdr_priority == ["ais"]


def test_pins_drop_blanks_and_unknown_services():
    assert normalize_sdr_pins({"op25": "00000001", "adsb": "", "ais": None, "x": "1"}) == {"op25": "00000001"}


def test_two_services_cannot_share_a_dongle():
    with pytest.raises(ValueError):
        normalize_sdr_pins({"op25": "69420", "adsb": "69420"})


@pytest.fixture
def node(tmp_path):
    """Isolated node_config.json + OP25 active.cfg.json, mocked decoders/op25/mqtt."""
    import app.internal.config_manager as cm
    from app.internal import sdr_settings as ss
    op25_cfg = tmp_path / "active.cfg.json"
    op25_cfg.write_text(json.dumps({"devices": [{"args": "rtl", "name": "sdr"}]}))
    with patch.object(cm, "_CONFIG_FILE", tmp_path / "node_config.json"), \
         patch.object(ss, "_OP25_CONFIG", op25_cfg), \
         patch.object(ss.secondary_sdr_client, "devices", AsyncMock(return_value=DEVS)), \
         patch.object(ss.secondary_sdr_client, "apply", AsyncMock(return_value=["adsb"])) as apply, \
         patch("app.internal.op25_client.op25_client.stop", AsyncMock()) as op25_stop, \
         patch("app.internal.op25_client.op25_client.start", AsyncMock()), \
         patch("app.internal.mqtt_manager.mqtt_manager.publish_checkin", AsyncMock()), \
         patch("asyncio.sleep", AsyncMock()):
        cm.save_node_config(_cfg())
        yield {"cm": cm, "ss": ss, "apply": apply, "op25_stop": op25_stop,
               "op25_args": lambda: json.loads(op25_cfg.read_text())["devices"][0]["args"]}


def test_unpinned_op25_is_still_opened_by_serial_of_the_first_dongle(node):
    assert asyncio.run(node["ss"].pin_op25_device()) == "69420"
    assert node["op25_args"]() == "rtl=69420"


def test_pinned_op25_opens_its_pinned_dongle(node):
    node["cm"].save_node_config(_cfg(sdr_pins={"op25": "00000001"}))
    asyncio.run(node["ss"].pin_op25_device())
    assert node["op25_args"]() == "rtl=00000001"


def test_shared_serial_leaves_op25_on_plain_rtl(node):
    dup = [dict(d, serial="00000001") for d in DEVS]
    with patch.object(node["ss"].secondary_sdr_client, "devices", AsyncMock(return_value=dup)):
        assert asyncio.run(node["ss"].pin_op25_device()) is None
    assert node["op25_args"]() == "rtl"


def test_priority_change_never_restarts_op25_and_reserves_its_dongle(node):
    node["cm"].save_node_config(_cfg(sdr_pins={"op25": "00000001"}))
    asyncio.run(node["ss"].set_sdr_settings(priority=["adsb", "ais"]))
    node["op25_stop"].assert_not_awaited()
    node["apply"].assert_awaited_with(["adsb", "ais"], {}, ["00000001"])


def test_moving_op25_restarts_it_on_the_new_dongle(node):
    asyncio.run(node["ss"].set_sdr_settings(priority=["adsb"], pins={"op25": "00000001", "adsb": "69420"}))
    node["op25_stop"].assert_awaited_once()
    assert node["op25_args"]() == "rtl=00000001"
    node["apply"].assert_awaited_with(["adsb"], {"adsb": "69420"}, ["00000001"])
    assert node["cm"].load_node_config().sdr_pins == {"op25": "00000001", "adsb": "69420"}
