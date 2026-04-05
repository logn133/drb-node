import httpx
from typing import Optional, Dict, Any
from app.config import settings
from app.internal.logger import logger


class OP25Client:
    def __init__(self):
        self.api_url = settings.op25_api_url
        self.terminal_url = settings.op25_terminal_url

    async def start(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(f"{self.api_url}/op25/start")
                r.raise_for_status()
                return True
        except Exception as e:
            logger.error(f"OP25 start failed: {e}")
            return False

    async def stop(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(f"{self.api_url}/op25/stop")
                r.raise_for_status()
                return True
        except Exception as e:
            logger.error(f"OP25 stop failed: {e}")
            return False

    async def status(self) -> Optional[Dict[str, Any]]:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(f"{self.api_url}/op25/status")
                r.raise_for_status()
                return r.json()
        except Exception as e:
            logger.error(f"OP25 status failed: {e}")
            return None

    async def generate_config(self, config: Dict[str, Any]) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(f"{self.api_url}/op25/generate-config", json=config)
                r.raise_for_status()
                return True
        except Exception as e:
            logger.error(f"OP25 generate-config failed: {e}")
            return False

    async def get_terminal_status(self) -> Optional[Any]:
        """Poll the OP25 HTTP terminal for current call metadata."""
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                r = await client.get(f"{self.terminal_url}/0/status.json")
                r.raise_for_status()
                return r.json()
        except Exception:
            return None


op25_client = OP25Client()
