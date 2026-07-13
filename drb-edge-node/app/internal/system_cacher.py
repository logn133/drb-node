import json
from pathlib import Path
from typing import List, Dict, Any, Optional
import httpx
from app.config import settings
from app.internal.logger import logger
from app.internal import credentials

_CACHE_FILE = Path(settings.config_path) / "systems_cache.json"

async def fetch_and_cache_systems() -> bool:
    """Fetch all systems from the C2 server and cache them locally."""
    if not settings.c2_url:
        logger.warning("C2_URL not configured. Skipping system caching.")
        return False

    url = f"{settings.c2_url}/systems"
    api_key = credentials.get_api_key()
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            systems = r.json()
            
            _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            _CACHE_FILE.write_text(json.dumps(systems, indent=2))
            logger.info(f"Cached {len(systems)} systems from C2.")
            return True
    except Exception as e:
        logger.warning(f"Failed to fetch systems from C2: {e}. Offline cache will be used.")
        return False

def load_cached_systems() -> List[Dict[str, Any]]:
    """Load cached systems from disk."""
    if _CACHE_FILE.exists():
        try:
            return json.loads(_CACHE_FILE.read_text())
        except Exception as e:
            logger.error(f"Failed to read systems cache: {e}")
    return []

def get_cached_system(system_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve a single system config from the cache."""
    systems = load_cached_systems()
    for sys in systems:
        if sys.get("system_id") == system_id:
            return sys
    return None
