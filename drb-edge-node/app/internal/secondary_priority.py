import asyncio
from typing import Iterable, List, Optional

from app.internal.config_manager import load_node_config, save_node_config
from app.internal.logger import logger
from app.internal.secondary_sdr_client import secondary_sdr_client
from app.models import normalize_secondary_priority


async def set_secondary_priority(priority: Iterable[str]) -> Optional[List[str]]:
    """Persist and apply the node's secondary SDR priority — one path for the
    local dashboard, a C2 command and a config push alike. Never touches op25:
    changing what the spare dongles do must not interrupt P25 recording.

    Returns the decoders now running (None if the container is unreachable).
    """
    ordered = normalize_secondary_priority(priority)
    cfg = load_node_config()
    cfg.secondary_sdr_priority = ordered
    cfg.secondary_sdr_mode = ordered[0] if ordered else "none"
    save_node_config(cfg)

    running = await secondary_sdr_client.apply(ordered)
    logger.info(f"Secondary SDR priority set to {ordered!r}; running {running!r}")

    # Report straight away so C2's view doesn't wait for the next heartbeat.
    from app.internal.mqtt_manager import mqtt_manager
    asyncio.create_task(mqtt_manager.publish_checkin())
    return running
