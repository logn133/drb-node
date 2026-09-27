"""
node-26#9 — secondary SDR priority: an ordered list the SDRs beyond op25's work
through. Normalisation, migration from the legacy single-mode field, and the one
apply path shared by the dashboard, C2 commands and config pushes.
"""
import asyncio
from unittest.mock import AsyncMock, patch

from app.models import NodeConfig, normalize_secondary_priority


def _cfg(**kw) -> NodeConfig:
    return NodeConfig(node_id="n1", node_name="N1", lat=0.0, lon=0.0, **kw)


def test_normalize_keeps_order_drops_unknown_and_duplicates():
    assert normalize_secondary_priority(["ais", "bogus", "adsb", "ais"]) == ["ais", "adsb"]
    assert normalize_secondary_priority([]) == []


def test_legacy_single_mode_migrates_to_priority():
    assert _cfg(secondary_sdr_mode="adsb").secondary_sdr_priority == ["adsb"]
    assert _cfg(secondary_sdr_mode="none").secondary_sdr_priority == []


def test_explicit_priority_wins_over_legacy_mode():
    assert _cfg(secondary_sdr_mode="adsb", secondary_sdr_priority=["ais"]).secondary_sdr_priority == ["ais"]


def test_set_priority_persists_applies_and_never_touches_op25(tmp_path):
    config_file = tmp_path / "node_config.json"
    import app.internal.config_manager as cm
    with patch.object(cm, "_CONFIG_FILE", config_file):
        cm.save_node_config(_cfg(secondary_sdr_mode="adsb"))
        from app.internal import secondary_priority as sp
        with patch.object(sp.secondary_sdr_client, "apply", AsyncMock(return_value=["ais"])) as apply, \
             patch("app.internal.mqtt_manager.mqtt_manager.publish_checkin", AsyncMock()), \
             patch("app.internal.op25_client.op25_client.stop", AsyncMock()) as op25_stop:
            running = asyncio.run(sp.set_secondary_priority(["ais", "adsb", "ais"]))
        saved = cm.load_node_config()

    assert running == ["ais"]
    apply.assert_awaited_once_with(["ais", "adsb"])
    op25_stop.assert_not_awaited()
    assert saved.secondary_sdr_priority == ["ais", "adsb"]
    assert saved.secondary_sdr_mode == "ais"


def test_clearing_priority_is_not_undone_by_legacy_migration(tmp_path):
    config_file = tmp_path / "node_config.json"
    import app.internal.config_manager as cm
    with patch.object(cm, "_CONFIG_FILE", config_file):
        cm.save_node_config(_cfg(secondary_sdr_mode="adsb"))
        from app.internal import secondary_priority as sp
        with patch.object(sp.secondary_sdr_client, "apply", AsyncMock(return_value=[])), \
             patch("app.internal.mqtt_manager.mqtt_manager.publish_checkin", AsyncMock()):
            asyncio.run(sp.set_secondary_priority([]))
        assert cm.load_node_config().secondary_sdr_priority == []
