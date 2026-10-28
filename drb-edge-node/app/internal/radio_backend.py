"""
Start/stop "the radio" without every caller knowing which recorder backend is
active (node-26#45). Every path that used to call op25_client.start/stop — C2
config pushes, the op25_restart command, the dashboard, SDR re-pinning — goes
through here.

With RECORDER_BACKEND=trunk-recorder, OP25 is never started: both open the
same RTL-SDR, and OP25 holding it would starve Trunk Recorder. start() becomes
a restart request to trunk-recorder/run.sh instead.
"""
import time
from typing import Optional

from app.internal import trunk_recorder as tr
from app.internal.logger import logger
from app.internal.op25_client import op25_client


def name() -> str:
    return "Trunk Recorder" if tr.enabled() else "OP25"


async def start(wait: bool = True) -> bool:
    return await start_with_reason(wait) is None


async def start_with_reason(wait: bool = True) -> Optional[str]:
    """None on success, else a human-readable reason.

    For Trunk Recorder, `wait` blocks until run.sh reports TR restarted (up
    to trunk_recorder.RESTART_WAIT_SECONDS), like op25_client.start returns
    only once OP25 has started — callers hand the spare dongles out next.
    """
    if not tr.enabled():
        return await op25_client.start_with_reason()
    if tr.trunk_recorder.config_error:
        return tr.trunk_recorder.config_error
    requested = time.time()
    if not tr.trunk_recorder.request_restart():
        return f"Couldn't write to {tr.config_dir()} to restart Trunk Recorder."
    if not wait:
        return None
    err = await tr.trunk_recorder.wait_started_since(requested)
    if err:
        logger.error(err)
    return err


async def stop() -> bool:
    if not tr.enabled():
        return await op25_client.stop()
    ok = tr.trunk_recorder.request_stop()
    if ok:
        logger.info("Trunk Recorder stop requested.")
    return ok
