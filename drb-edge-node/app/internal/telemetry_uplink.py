import asyncio

import httpx

from app.config import settings
from app.internal import credentials
from app.internal.config_manager import load_node_config
from app.internal.logger import logger
from app.internal.secondary_sdr_client import secondary_sdr_client

# How often the second-SDR decoder's current snapshot is forwarded to C2
# (node-26#9). This is a live-map overlay, not a flight/vessel history, so
# there is no backlog/retry on a missed tick — the next one supersedes it.
UPLINK_INTERVAL_SECONDS = 10


async def _post_snapshot(path: str, body: dict) -> None:
    if not settings.c2_url:
        return
    api_key = credentials.get_api_key()
    if not api_key:
        return
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{settings.c2_url}{path}", json=body, headers=headers)
            r.raise_for_status()
    except Exception as e:
        logger.debug(f"Telemetry uplink to {path} failed: {e}")


async def telemetry_uplink_loop():
    while True:
        await asyncio.sleep(UPLINK_INTERVAL_SECONDS)
        config = load_node_config()
        if config.secondary_sdr_mode != "adsb":
            continue
        snapshot = await secondary_sdr_client.data()
        if not snapshot or not snapshot.get("aircraft"):
            continue
        await _post_snapshot("/telemetry/adsb", {"aircraft": snapshot["aircraft"]})
