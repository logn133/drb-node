"""
Unit tests for config_manager — pure file I/O, no external services.
"""
import json
import pytest
from pathlib import Path
from unittest.mock import patch
from app.models import NodeConfig, SystemConfig


# ---------------------------------------------------------------------------
# save / load round-trip
# ---------------------------------------------------------------------------

def test_save_and_load_roundtrip(tmp_path):
    config = NodeConfig(
        node_id="test-node-01",
        node_name="Test Node",
        lat=40.7128,
        lon=-74.0060,
        configured=True,
        assigned_system_id="sys-001",
    )

    config_file = tmp_path / "node_config.json"
    with patch("app.internal.config_manager._CONFIG_FILE", config_file):
        import app.internal.config_manager as cm
        cm.save_node_config(config)
        loaded = cm.load_node_config()

    assert loaded.node_id == config.node_id
    assert loaded.node_name == config.node_name
    assert loaded.lat == pytest.approx(config.lat)
    assert loaded.lon == pytest.approx(config.lon)
    assert loaded.configured is True
    assert loaded.assigned_system_id == "sys-001"


def test_save_writes_valid_json(tmp_path):
    config = NodeConfig(
        node_id="node-x",
        node_name="X",
        lat=1.0,
        lon=2.0,
    )
    config_file = tmp_path / "node_config.json"
    with patch("app.internal.config_manager._CONFIG_FILE", config_file):
        import app.internal.config_manager as cm
        cm.save_node_config(config)

    data = json.loads(config_file.read_text())
    assert data["node_id"] == "node-x"
    assert data["configured"] is False


# ---------------------------------------------------------------------------
# load fallback behaviour
# ---------------------------------------------------------------------------

def test_load_missing_file_returns_defaults(tmp_path):
    config_file = tmp_path / "nonexistent.json"
    with patch("app.internal.config_manager._CONFIG_FILE", config_file):
        import app.internal.config_manager as cm
        result = cm.load_node_config()

    assert result.configured is False


def test_load_invalid_json_returns_defaults(tmp_path):
    config_file = tmp_path / "node_config.json"
    config_file.write_text("not valid json {{{")

    with patch("app.internal.config_manager._CONFIG_FILE", config_file):
        import app.internal.config_manager as cm
        result = cm.load_node_config()

    assert result.configured is False


def test_load_partial_json_returns_defaults(tmp_path):
    """A truncated file (e.g. mid-write crash) should fall back to defaults."""
    config_file = tmp_path / "node_config.json"
    config_file.write_text('{"node_id": "x"')  # truncated

    with patch("app.internal.config_manager._CONFIG_FILE", config_file):
        import app.internal.config_manager as cm
        result = cm.load_node_config()

    assert result.configured is False


# ---------------------------------------------------------------------------
# apply_system_config
# ---------------------------------------------------------------------------

def test_apply_system_config_writes_json(tmp_path):
    system = SystemConfig(
        system_id="sys-001",
        name="Test System",
        type="P25",
        config={"freq": 851.0125, "nac": 0x293},
    )

    with patch("app.internal.config_manager.settings") as mock_settings:
        mock_settings.config_path = str(tmp_path)
        import app.internal.config_manager as cm
        result = cm.apply_system_config(system)

    assert result is True
    written = json.loads((tmp_path / "active.cfg.json").read_text())
    assert written["freq"] == pytest.approx(851.0125)
    assert written["nac"] == 0x293


def test_apply_system_config_returns_false_on_error(tmp_path):
    system = SystemConfig(
        system_id="sys-001",
        name="Test",
        type="DMR",
        config={},
    )

    with patch("app.internal.config_manager.settings") as mock_settings:
        # Point at a path that can't be written (a file, not a dir)
        blocker = tmp_path / "blocker"
        blocker.touch()
        mock_settings.config_path = str(blocker)
        import app.internal.config_manager as cm
        result = cm.apply_system_config(system)

    assert result is False
