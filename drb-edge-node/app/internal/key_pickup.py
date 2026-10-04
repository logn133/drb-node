"""
HTTPS api_key pickup — the node's own half of C2's enrollment flow
(Server/drb-c2-core/app/routers/enrollment.py, GET /nodes/{id}/credentials).

Why this exists (node-26#14): key delivery used to be MQTT-only
(nodes/{id}/api_key), and a dynsec broker refuses a node that has no key — or
a stale one — so a locked-out node could never receive the key that would let
it in. install.sh already did this pickup once at install time; doing it from
the running app too means a node approved after install, or whose key was
reissued, recovers on its own with no SSH and no container restart.

Auth is the pickup secret install.sh saved to /configs/pickup_secret at
enrollment (the same file, bind-mounted). Only ever sent to settings.c2_url,
over whatever scheme the operator configured (https in production).
"""
from typing import Optional

import httpx

from app.config import settings
from app.internal import credentials
from app.internal.logger import logger

_TIMEOUT = 15


async def fetch_api_key() -> Optional[str]:
    """Ask C2 for this node's api_key. Returns it, or None when there isn't one
    to be had yet (not approved, no pickup secret, C2 unreachable, rejected).
    Never raises — the caller retries on its own backoff."""
    if not settings.c2_url:
        logger.warning("Key pickup skipped: C2_URL is not configured.")
        return None
    secret = credentials.get_pickup_secret()
    if not secret:
        logger.warning(
            f"Key pickup skipped: no {settings.config_path}/pickup_secret. This node was not enrolled by "
            "install.sh on this machine — re-run install.sh, or have an admin Reissue key and write it in."
        )
        return None

    url = f"{settings.c2_url.rstrip('/')}/nodes/{settings.node_id}/credentials"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(url, headers={"X-Pickup-Secret": secret})
    except Exception as e:
        logger.warning(f"Key pickup: could not reach C2 ({e}).")
        return None

    if r.status_code == 401:
        logger.error(
            "Key pickup: C2 rejected the saved pickup secret (401) — it was rotated by a re-enroll elsewhere. "
            "Re-run install.sh with --token to recover."
        )
        return None
    if r.status_code == 404:
        logger.error("Key pickup: C2 does not know this node (404) — it was deleted server-side. Re-run install.sh.")
        return None
    if r.status_code != 200:
        logger.warning(f"Key pickup: unexpected HTTP {r.status_code} from C2.")
        return None

    try:
        body = r.json()
    except ValueError:
        logger.warning("Key pickup: C2 returned a non-JSON body.")
        return None
    key = body.get("api_key") if isinstance(body, dict) else None
    if not key or not isinstance(key, str):
        status = body.get("approval_status") if isinstance(body, dict) else None
        logger.info(f"Key pickup: no api_key yet (approval_status={status!r}) — waiting for an admin to approve.")
        return None
    return key
