import httpx
from typing import Any, Dict, Optional
from app.config import settings
from app.internal.logger import logger


class SecondarySdrClient:
    """Talks to the secondary-sdr-container (node-26#9) over its control API.

    Mirrors op25_client.py's shape on purpose — same failure handling (log
    and return None/False rather than raise), since this container is
    optional and its absence must never break the primary op25 radio path.
    """

    def __init__(self):
        self.api_url = settings.secondary_sdr_api_url

    async def start(self, mode: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(f"{self.api_url}/secondary/start", json={"mode": mode})
                r.raise_for_status()
                return True
        except Exception as e:
            logger.error(f"Secondary SDR start (mode={mode!r}) failed: {e}")
            return False

    async def stop(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(f"{self.api_url}/secondary/stop")
                r.raise_for_status()
                return True
        except Exception as e:
            logger.error(f"Secondary SDR stop failed: {e}")
            return False

    async def status(self) -> Optional[Dict[str, Any]]:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(f"{self.api_url}/secondary/status")
                r.raise_for_status()
                return r.json()
        except Exception as e:
            logger.error(f"Secondary SDR status failed: {e}")
            return None

    async def data(self) -> Optional[Dict[str, Any]]:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(f"{self.api_url}/secondary/data")
                r.raise_for_status()
                return r.json()
        except Exception as e:
            logger.error(f"Secondary SDR data fetch failed: {e}")
            return None


secondary_sdr_client = SecondarySdrClient()
