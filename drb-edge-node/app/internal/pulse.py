"""
PulseAudio readiness helpers.

The PulseAudio daemon lives in the `op25` container and exposes its native
socket on the shared `pulse_socket` docker volume (mounted at /run/pulse in
both containers, with PULSE_SERVER=unix:/run/pulse/native).

`op25-container/docker-entrypoint.sh` waits up to ~10 s for that socket before
starting its own app, but the edge-node historically had *no* equivalent wait:
FFmpeg would be launched with `-f pulse` before the socket existed, fail
instantly, and the audio path would stay dead for the lifetime of the process.
This module is the missing wait.

NOTE on the source name: the op25 entrypoint starts pulseaudio with `-n`, which
skips /etc/pulse/system.pa entirely and loads modules from the command line
instead.  That means the `set-default-source drb_sink.monitor` line in system.pa
is NOT applied at runtime, so `-i default` is unreliable.  Always address the
monitor explicitly via settings.pulse_source (default "drb_sink.monitor").
"""
import asyncio
import os
import stat
from typing import Optional

from app.config import settings
from app.internal.logger import logger

DEFAULT_SOCKET_PATH = "/run/pulse/native"
POLL_INTERVAL = 0.5


def socket_path() -> str:
    """Resolve the PulseAudio socket path from PULSE_SERVER (`unix:/path` form)."""
    server = os.environ.get("PULSE_SERVER", "")
    if server.startswith("unix:"):
        candidate = server[len("unix:"):].strip()
        if candidate:
            return candidate
    return DEFAULT_SOCKET_PATH


def is_ready() -> bool:
    """True when the PulseAudio native socket exists and really is a socket."""
    try:
        return stat.S_ISSOCK(os.stat(socket_path()).st_mode)
    except OSError:
        return False


async def wait_until_ready(timeout: Optional[float] = None) -> bool:
    """
    Block until the PulseAudio socket appears, or `timeout` seconds elapse.

    Bounded on purpose — never hang the caller forever.  Returns True if the
    socket is present, False on timeout (caller decides whether to retry).
    """
    limit = settings.pulse_wait_timeout if timeout is None else timeout
    path = socket_path()

    if is_ready():
        return True

    logger.info(f"Waiting up to {limit:.0f}s for PulseAudio socket at {path}…")
    waited = 0.0
    while waited < limit:
        await asyncio.sleep(POLL_INTERVAL)
        waited += POLL_INTERVAL
        if is_ready():
            logger.info(f"PulseAudio socket ready after {waited:.1f}s.")
            return True

    logger.error(
        f"PulseAudio socket {path} not present after {limit:.0f}s — "
        "is the op25 container running? Audio capture will retry."
    )
    return False
