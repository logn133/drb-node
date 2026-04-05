import json
from pathlib import Path
from typing import Optional
from app.config import settings
from app.models import NodeConfig, SystemConfig
from app.internal.logger import logger

_CONFIG_FILE = Path(settings.config_path) / "node_config.json"


def load_node_config() -> NodeConfig:
    if _CONFIG_FILE.exists():
        try:
            data = json.loads(_CONFIG_FILE.read_text())
            return NodeConfig(**data)
        except Exception as e:
            logger.warning(f"Could not load node config, using defaults: {e}")

    return NodeConfig(
        node_id=settings.node_id,
        node_name=settings.node_name,
        lat=settings.node_lat,
        lon=settings.node_lon,
        configured=False,
    )


def save_node_config(config: NodeConfig) -> None:
    _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_FILE.write_text(config.model_dump_json(indent=2))
    logger.info("Node config saved.")


def apply_system_config(system_config: SystemConfig) -> bool:
    """Write the OP25-compatible config blob to /configs/active.cfg.json."""
    try:
        active_cfg = Path(settings.config_path) / "active.cfg.json"
        active_cfg.write_text(json.dumps(system_config.config, indent=2))
        logger.info(f"System config applied: {system_config.name}")
        return True
    except Exception as e:
        logger.error(f"Failed to write system config: {e}")
        return False
