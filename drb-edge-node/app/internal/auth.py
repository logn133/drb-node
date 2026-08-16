"""
Local dashboard / API authentication.

These edge nodes are deployed at arbitrary third-party locations and serve
both an HTML dashboard and a JSON API on the same FastAPI app (port 80,
network_mode: host) — anyone on that site's LAN can otherwise reach every
control endpoint. This module adds username/password auth in front of it.

Design:
  - Username + password come from app/config.py (DASHBOARD_USERNAME /
    DASHBOARD_PASSWORD env vars), with a first-boot default that MUST be
    changed — see is_using_default_password() and its call site in main.py.
  - The password is never compared or stored in plaintext. It's hashed with
    stdlib hashlib.scrypt (no new dependency — this image runs on a Raspberry
    Pi) using a salt generated once on first boot and persisted via
    app/internal/credentials.py, then compared with hmac.compare_digest.
  - Two auth paths, both accepted on every protected route:
      * Browser dashboard: a signed session cookie set by POST /login
        (HMAC-SHA256 over "username:expiry", no server-side session store —
        the signing key is the persisted session secret from credentials.py).
      * Machine callers: HTTP Basic with the same username/password. As of
        this writing no non-browser caller of this node's own API was found
        anywhere in Client/ or Server/ (nodes are only ever reached over MQTT
        + node-initiated outbound HTTP to C2, never the other way around) —
        Basic is kept anyway as a stateless fallback for curl/scripts in the
        field, since it needs no login flow and costs little to support.

Caveat worth knowing: this node's dashboard is plain HTTP (no TLS
termination on :80), so both the session cookie and Basic credentials travel
unencrypted on the local network either way. Auth here stops a passerby from
opening the dashboard and pressing buttons; it does not stop a LAN-level
sniffer. That would need TLS in front of the node, which is out of scope here.
"""
import base64
import hashlib
import hmac
import time
from typing import Optional

from fastapi import Cookie, Header, HTTPException, status

from app.config import settings
from app.internal import credentials
from app.internal.logger import logger

SESSION_COOKIE_NAME = "drb_node_session"
# 12h: long enough that the dashboard doesn't demand a daily re-login on a
# device left open on someone's desk, short enough that a stolen cookie isn't
# valid forever.
SESSION_TTL_SECONDS = 12 * 60 * 60

# scrypt cost parameters. n=2**14 (16384) keeps the derivation well under the
# ~1s ballpark on a Raspberry Pi's memory/CPU budget — this only runs on
# login attempts (rare), never on the hot path.
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32

# Kept in sync with app/config.py's Settings.dashboard_password default.
DEFAULT_PASSWORD = "CHANGE-ME-drb-default"


def _hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )


def is_using_default_password() -> bool:
    return settings.dashboard_password == DEFAULT_PASSWORD


def verify_credentials(username: str, password: str) -> bool:
    """Constant-time check of a submitted username/password against config."""
    salt = credentials.get_auth_salt()
    expected_hash = _hash_password(settings.dashboard_password, salt)
    submitted_hash = _hash_password(password, salt)

    user_ok = hmac.compare_digest(
        username.encode("utf-8"), settings.dashboard_username.encode("utf-8")
    )
    pass_ok = hmac.compare_digest(submitted_hash, expected_hash)
    return user_ok and pass_ok


def _sign(payload: str) -> str:
    secret = credentials.get_session_secret()
    return hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def create_session_token(username: str) -> str:
    """Build a signed, expiring, opaque session token (no server-side state)."""
    expiry = int(time.time()) + SESSION_TTL_SECONDS
    payload = f"{username}:{expiry}"
    sig = _sign(payload)
    raw = f"{payload}:{sig}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("utf-8")


def _verify_session_token(token: str) -> Optional[str]:
    try:
        raw = base64.urlsafe_b64decode(token.encode("utf-8")).decode("utf-8")
        username, expiry_s, sig = raw.rsplit(":", 2)
        expiry = int(expiry_s)
    except Exception:
        return None

    expected_sig = _sign(f"{username}:{expiry_s}")
    if not hmac.compare_digest(sig, expected_sig):
        return None
    if time.time() > expiry:
        return None
    if not hmac.compare_digest(
        username.encode("utf-8"), settings.dashboard_username.encode("utf-8")
    ):
        return None
    return username


def _verify_basic_auth(header_value: str) -> bool:
    try:
        scheme, _, encoded = header_value.partition(" ")
        if scheme.lower() != "basic":
            return False
        decoded = base64.b64decode(encoded).decode("utf-8")
        username, _, password = decoded.partition(":")
    except Exception:
        return False
    return verify_credentials(username, password)


def is_authenticated(
    session_cookie: Optional[str], authorization: Optional[str]
) -> bool:
    if session_cookie and _verify_session_token(session_cookie):
        return True
    if authorization and _verify_basic_auth(authorization):
        return True
    return False


async def require_session(
    drb_node_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> bool:
    """Dependency for dashboard HTML pages. Returns False rather than raising
    so the route can redirect to /login instead of showing a bare 401."""
    return bool(drb_node_session and _verify_session_token(drb_node_session))


async def require_auth(
    drb_node_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    authorization: Optional[str] = Header(default=None),
) -> None:
    """Dependency for /api/* routes — session cookie (dashboard's own fetch
    calls) or HTTP Basic (machine callers) both satisfy it."""
    if is_authenticated(drb_node_session, authorization):
        return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
        headers={"WWW-Authenticate": "Basic"},
    )


def warn_if_default_password() -> None:
    if is_using_default_password():
        logger.warning(
            "DASHBOARD_PASSWORD is still the first-boot default — "
            "set DASHBOARD_USERNAME/DASHBOARD_PASSWORD in .env before this "
            "node leaves the bench. Anyone on the node's LAN can currently "
            "log in with the default credentials."
        )
