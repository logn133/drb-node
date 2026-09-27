import httpx
from typing import Any, Dict, List, Optional
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

    async def devices(self) -> Optional[List[Dict[str, Any]]]:
        """Every RTL-SDR on the node with its serial, or None if unreachable."""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(f"{self.api_url}/secondary/devices")
                r.raise_for_status()
                return r.json().get("devices", [])
        except Exception as e:
            logger.error(f"Secondary SDR device list failed: {e}")
            return None

    async def apply(self, priority: List[str], pins: Dict[str, str], reserved: List[str]) -> Optional[List[str]]:
        """Run decoders down the priority list until SDRs run out, honouring
        pins and never touching `reserved` (op25's) dongles. Returns the modes
        actually running, or None if the container is unreachable."""
        body = {"priority": priority, "pins": pins, "reserved": reserved}
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(f"{self.api_url}/secondary/apply", json=body)
                r.raise_for_status()
                return r.json().get("running", [])
        except Exception as e:
            logger.error(f"Secondary SDR apply (priority={priority!r}) failed: {e}")
            return None

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
