"""
Manages the persisted node API key, plus the local-auth signing material used
by app/internal/auth.py.

The API key is provisioned by the C2 server after an admin approves the node.
It arrives via MQTT and is saved to /configs/credentials.json so it survives
container restarts.

The scrypt salt and session-signing secret are generated locally on first boot
(never provisioned externally) and persisted the same way, so dashboard
sessions survive a container restart instead of forcing every operator to
re-login whenever the node restarts.
"""
import json
import secrets
from pathlib import Path
from app.config import settings
from app.internal.logger import logger

_CREDS_FILE = Path(settings.config_path) / "credentials.json"
_api_key: str | None = None
_auth_salt: bytes | None = None
_session_secret: bytes | None = None


def load() -> None:
    """Load persisted credentials from disk on startup."""
    global _api_key, _auth_salt, _session_secret
    if _CREDS_FILE.exists():
        try:
            data = json.loads(_CREDS_FILE.read_text())
            _api_key = data.get("api_key")
            if data.get("auth_salt"):
                _auth_salt = bytes.fromhex(data["auth_salt"])
            if data.get("session_secret"):
                _session_secret = bytes.fromhex(data["session_secret"])
            if _api_key:
                logger.info("Node credentials loaded from disk.")
        except Exception as e:
            logger.warning(f"Could not read credentials file: {e}")
    _ensure_auth_material()


def _ensure_auth_material() -> None:
    """Generate (once) and persist the local-auth salt + session secret."""
    global _auth_salt, _session_secret
    changed = False
    if _auth_salt is None:
        _auth_salt = secrets.token_bytes(16)
        changed = True
    if _session_secret is None:
        _session_secret = secrets.token_bytes(32)
        changed = True
    if changed:
        _write()
        logger.info("Generated local-auth signing material (first boot).")


def get_auth_salt() -> bytes:
    """Scrypt salt for dashboard password hashing — generated once, persisted."""
    if _auth_salt is None:
        _ensure_auth_material()
    return _auth_salt  # type: ignore[return-value]


def get_session_secret() -> bytes:
    """HMAC key used to sign dashboard session cookies — generated once, persisted."""
    if _session_secret is None:
        _ensure_auth_material()
    return _session_secret  # type: ignore[return-value]


def get_api_key() -> str | None:
    return _api_key


def save_api_key(key: str) -> None:
    global _api_key
    _api_key = key
    _write()
    logger.info("Node API key saved to disk.")


def _write() -> None:
    _CREDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {"api_key": _api_key}
    if _auth_salt is not None:
        data["auth_salt"] = _auth_salt.hex()
    if _session_secret is not None:
        data["session_secret"] = _session_secret.hex()
    _CREDS_FILE.write_text(json.dumps(data))
