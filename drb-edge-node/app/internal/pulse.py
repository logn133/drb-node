"""
PulseAudio readiness helpers.

The PulseAudio daemon lives in the `op25` container and exposes its native
socket on the shared `pulse_socket` docker volume (mounted at /run/pulse in
both containers, with PULSE_SERVER=unix:/run/pulse/native).

`op25-container/docker-entrypoint.sh` waits (bounded) for that daemon to
actually answer before starting its own app, and the edge-node needs the same
guarantee before launching FFmpeg: FFmpeg with `-f pulse` fails instantly if
nothing is listening, and used to stay dead for the lifetime of the process.
This module is the wait.

HISTORY / WHY THIS CHECKS LIVENESS, NOT FILE EXISTENCE: the `pulse_socket`
named volume survives container recreation, but the daemon process that
created the socket does not. Observed on live hardware: a stale
`/run/pulse/native` socket file and `/run/pulse/pid` from a killed daemon
were still in the volume after `docker compose up -d --build` recreated the
containers. PulseAudio refused to start ("Daemon already running") because of
the stale pid file, so nothing was actually listening on the socket — but the
socket *file* still existed. An earlier version of this module (and of the
op25 entrypoint) only checked `stat.S_ISSOCK` on the path, so it reported
"ready" against a dead daemon, FFmpeg launched anyway, and immediately failed
with "No such process" in a tight restart loop. Readiness here means "a
PulseAudio connection actually succeeds," never "a file exists at this path."

NOTE on the source name: the op25 entrypoint starts pulseaudio with `-n`, which
skips /etc/pulse/system.pa entirely and loads modules from the command line
instead.  That means the `set-default-source drb_sink.monitor` line in system.pa
is NOT applied at runtime, so `-i default` is unreliable.  Always address the
monitor explicitly via settings.pulse_source (default "drb_sink.monitor").
"""
import asyncio
import os
import shutil
import subprocess
from typing import Optional

from app.config import settings
from app.internal.logger import logger

DEFAULT_SOCKET_PATH = "/run/pulse/native"
POLL_INTERVAL = 0.5

# Bounded timeout for a single `pactl info` liveness probe. Kept short: this
# runs synchronously on the calling thread (see is_ready()), and callers of
# is_ready() include a sync code path inside the Discord voice bot, so a slow
# probe would stall its event loop. wait_until_ready() runs probes off-thread
# via asyncio.to_thread and can afford this bound comfortably within its own
# much larger PULSE_WAIT_TIMEOUT.
PROBE_TIMEOUT_SECONDS = 1.5


def socket_path() -> str:
    """Resolve the PulseAudio socket path from PULSE_SERVER (`unix:/path` form)."""
    server = os.environ.get("PULSE_SERVER", "")
    if server.startswith("unix:"):
        candidate = server[len("unix:"):].strip()
        if candidate:
            return candidate
    return DEFAULT_SOCKET_PATH


def _probe_env(path: str) -> dict:
    env = dict(os.environ)
    env["PULSE_SERVER"] = f"unix:{path}"
    return env


def _daemon_responds() -> bool:
    """
    True only when a PulseAudio daemon actually answers on the configured
    socket. Shells out to `pactl info` (from `pulseaudio-utils`, installed
    alongside `libpulse0` in the edge-node image) rather than re-implementing
    the native protocol handshake in Python — this container has no other use
    for talking to PulseAudio directly, so a subprocess call is the smallest
    correct implementation.

    Deliberately does NOT check `os.path.exists`/`stat.S_ISSOCK` first: a
    stale socket file from a killed daemon passes that check and always did,
    which is the exact defect this function replaces.
    """
    path = socket_path()
    pactl = shutil.which("pactl")
    if pactl is None:
        logger.error("pactl not found in PATH — cannot verify PulseAudio liveness.")
        return False
    try:
        result = subprocess.run(
            [pactl, "info"],
            env=_probe_env(path),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def is_ready() -> bool:
    """
    True when a PulseAudio daemon is alive and answering right now.

    Synchronous and bounded by PROBE_TIMEOUT_SECONDS — used from a sync call
    site (discord_radio._play_stream). Prefer `wait_until_ready()` from async
    code so the probe doesn't block the event loop.
    """
    return _daemon_responds()


async def wait_until_ready(timeout: Optional[float] = None) -> bool:
    """
    Block until a PulseAudio daemon actually answers, or `timeout` seconds
    elapse.

    Bounded on purpose — never hang the caller forever. Returns True once a
    live connection succeeds, False on timeout (caller decides whether to
    retry). Each probe runs via asyncio.to_thread so the subprocess call never
    blocks the event loop.
    """
    limit = settings.pulse_wait_timeout if timeout is None else timeout
    path = socket_path()

    if await asyncio.to_thread(_daemon_responds):
        return True

    logger.info(f"Waiting up to {limit:.0f}s for a live PulseAudio daemon at {path}…")
    waited = 0.0
    while waited < limit:
        await asyncio.sleep(POLL_INTERVAL)
        waited += POLL_INTERVAL
        if await asyncio.to_thread(_daemon_responds):
            logger.info(f"PulseAudio daemon live after {waited:.1f}s.")
            return True

    logger.error(
        f"PulseAudio daemon at {path} not responding after {limit:.0f}s — "
        "is the op25 container's daemon actually up? Audio capture will retry."
    )
    return False
