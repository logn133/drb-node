"""
Manages the persisted node API key, plus the local-auth signing material used
by app/internal/auth.py.

The API key is provisioned by the C2 server after an admin approves the node.
install.sh fetches it over HTTPS (GET /nodes/{id}/credentials + the pickup
secret it saved to /configs/pickup_secret) and writes /configs/credentials.json;
the running app can also fetch it itself (app/internal/key_pickup.py) and
re-read the file at any time (reload_api_key), so a key written after startup
is picked up without a container restart (node-26#14). The legacy MQTT
delivery (nodes/{id}/api_key) still saves here too.

The scrypt salt and session-signing secret are generated locally on first boot
(never provisioned externally) and persisted the same way, so dashboard
sessions survive a container restart instead of forcing every operator to
re-login whenever the node restarts.
"""
import json
import os
import secrets
from pathlib import Path
from app.config import settings
from app.internal.logger import logger

_CREDS_FILE = Path(settings.config_path) / "credentials.json"
_PICKUP_FILE = Path(settings.config_path) / "pickup_secret"
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


def reload_api_key() -> str | None:
    """Re-read the api_key from disk (install.sh, or an operator, may have
    written it after startup). Returns the key now in effect. Only the api_key
    is taken from the file — the auth material already in memory stays."""
    global _api_key
    try:
        data = json.loads(_CREDS_FILE.read_text())
    except FileNotFoundError:
        return _api_key
    except Exception as e:
        logger.warning(f"Could not re-read credentials file: {e}")
        return _api_key
    key = data.get("api_key")
    if key and key != _api_key:
        _api_key = key
        logger.info("Node API key re-read from disk (changed since it was last loaded).")
    return _api_key


def get_pickup_secret() -> str | None:
    """The one-time pickup secret install.sh saved at enrollment, if any."""
    try:
        secret = _PICKUP_FILE.read_text().strip()
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning(f"Could not read pickup secret: {e}")
        return None
    return secret or None


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
    # Atomic replace with 0600: install.sh on the host writes this same file,
    # and a torn half-written JSON would lose the key AND the auth material.
    tmp = _CREDS_FILE.with_name(_CREDS_FILE.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(data))
    os.replace(tmp, _CREDS_FILE)
