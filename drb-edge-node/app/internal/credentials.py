"""
Manages the persisted node API key.

The key is provisioned by the C2 server after an admin approves the node.
It arrives via MQTT and is saved to /configs/credentials.json so it survives
container restarts.
"""
import json
from pathlib import Path
from app.config import settings
from app.internal.logger import logger

_CREDS_FILE = Path(settings.config_path) / "credentials.json"
_api_key: str | None = None


def load() -> None:
    """Load persisted credentials from disk on startup."""
    global _api_key
    if _CREDS_FILE.exists():
        try:
            data = json.loads(_CREDS_FILE.read_text())
            _api_key = data.get("api_key")
            if _api_key:
                logger.info("Node credentials loaded from disk.")
        except Exception as e:
            logger.warning(f"Could not read credentials file: {e}")


def get_api_key() -> str | None:
    return _api_key


def save_api_key(key: str) -> None:
    global _api_key
    _api_key = key
    _CREDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CREDS_FILE.write_text(json.dumps({"api_key": key}))
    logger.info("Node API key saved to disk.")
