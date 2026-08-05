"""
Event-driven call state machine.

Replaces the old hang-counter inference (which derived call start from "a tgid
appeared in channel_update" and call end from N polls of silence) with the two
authoritative signals OP25 actually exposes:

  START — a `call_log` entry. OP25 appends one at channel-grant time stamped with
          its own time.time(). This is an exact start timestamp, not the moment
          our poll happened to notice, so recordings can be sliced back to it.

  END   — the `srcaddr` != 0 → `srcaddr` == 0 transition in `channel_update`.
          OP25 never reports call termination externally: internally it ends a
          call on the P25 Terminator Data Unit (duid15) or 3 voice-framing
          timeouts, but neither becomes a log entry. What *is* observable is that
          `srcaddr`/`svcopts` reset to 0/false the instant the call ends, while
          `tgid`/`hold_tgid` keep showing the just-ended talkgroup for
          TGID_HOLD_TIME (2 s). So the srcaddr edge is a real state change, not a
          timeout heuristic.

SEGMENTS: one emitted call (= one recording, one Firestore doc) spans a whole
conversation, not a single transmission. It stays open across repeated grants on
the same talkgroup and closes when the talkgroup changes or the radio goes quiet
for settings.call_idle_timeout seconds.

CLOCKS: `call_log["time"]` is time.time() inside the op25 container. All three
client containers run network_mode: host and share the host kernel clock, so that
value is directly comparable to time.time() here — no offset mapping needed. The
call recorder's ring buffer is stamped with the same clock for the same reason.
"""
import asyncio
import time
import uuid
from datetime import datetime, timezone
from typing import Optional, Callable, Awaitable, Any, List, Dict

from app.config import settings
from app.internal.op25_client import op25_client
from app.internal.logger import logger

CallbackFn = Callable[[dict], Awaitable[None]]

# 500 ms. Do NOT lower: start precision already comes from OP25's own timestamp,
# and http_server.py's request handler has a ~200 ms blocking floor anyway.
POLL_INTERVAL = 0.5

# Seconds of unreachable OP25 before an open segment is force-closed.
OP25_OFFLINE_GRACE = 3.0

# Hard ceiling on a single segment; mirrors MAX_RECORDING_SECONDS in call_recorder
# so a talkgroup that never goes quiet cannot produce an unbounded recording.
MAX_SEGMENT_SECONDS = 600


def _tail_pad() -> float:
    """
    Audio kept after the observed end of the last transmission, so the srcaddr
    edge (up to one poll late) plus encoder latency never clips the tail.

    Read live from settings (env CALL_TAIL_PAD_SECONDS) rather than frozen into a
    module constant, so it is tunable per node. See the setting in config.py for
    why the default moved 1.0 → 3.0 (short calls' recording window was closing
    before the ~1.5s grant→speech offset let voice audio even start).

    Only the idle-timeout close path below uses this pad — the tgid_change and
    tgid_change_unlogged paths close at an exact, already-known boundary (the
    new grant's timestamp, or the same poll tick) and intentionally add none.
    """
    return settings.call_tail_pad_seconds


def _as_int(value: Any) -> Optional[int]:
    """Coerce an OP25 field to a positive int, or None. Rejects 0/""/"None"."""
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iso(epoch: Optional[float]) -> Optional[str]:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


class MetadataWatcher:
    def __init__(self):
        self._running = False

        # Open segment state
        self._active_call_id: Optional[str] = None
        self._current_tgid: Optional[int] = None
        self._current_tgid_name: Optional[str] = None
        self._current_freq: Any = None
        self._current_srcaddr: Optional[int] = None
        self._started_at: Optional[float] = None   # OP25 epoch of the first grant
        self._transmissions: int = 0

        # Transmission tracking within the open segment
        self._tx_active: bool = False              # last poll saw srcaddr != 0
        self._last_activity: float = 0.0           # epoch of last evidence of traffic
        self._last_tx_end: Optional[float] = None  # epoch of the srcaddr 1→0 edge
        self._last_ok_poll: float = 0.0

        # Injectable for tests; production is always the host wall clock.
        self._clock: Callable[[], float] = time.time

        # Set these before calling start()
        self.on_call_start: Optional[CallbackFn] = None
        self.on_call_end: Optional[CallbackFn] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        self._running = True
        self._last_ok_poll = self._clock()
        asyncio.create_task(self._poll_loop())
        logger.info("Metadata watcher started (call_log driven).")

    async def stop(self):
        self._running = False
        if self._active_call_id:
            await self._close_segment(self._clock(), reason="shutdown")

    async def _poll_loop(self):
        while self._running:
            try:
                await self._tick()
            except Exception as e:
                logger.warning(f"Metadata poll error: {e}")
            await asyncio.sleep(POLL_INTERVAL)

    # ------------------------------------------------------------------
    # One poll
    # ------------------------------------------------------------------

    async def _tick(self):
        now = self._clock()
        update = await op25_client.poll_terminal()

        if update is None:
            # OP25 unreachable. Don't kill an open segment on a single blip.
            if self._active_call_id and (now - self._last_ok_poll) >= OP25_OFFLINE_GRACE:
                await self._close_segment(now, reason="op25_unreachable")
            return

        self._last_ok_poll = now

        # 1. call_log first — these are the authoritative starts, and processing
        #    them before the channel scan means a same-poll grant+state pair is
        #    already attributed to the new segment by the time we scan channels.
        #    Sorted defensively: multi-receiver setups append per receiver.
        for entry in sorted(update.call_log, key=lambda e: _as_float(e.get("time")) or 0.0):
            await self._handle_call_log(entry, now)

        # 2. channel_update — the only external end signal.
        await self._handle_channels(update.channels, now)

    async def _handle_call_log(self, entry: Dict[str, Any], now: float) -> None:
        tgid = _as_int(entry.get("tgid"))
        if tgid is None:
            return  # a grant with no talkgroup is nothing we can record or label

        # OP25's own stamp. Fall back to now only if the field is missing/garbage.
        started_at = _as_float(entry.get("time"))
        if started_at is None:
            logger.warning(f"call_log entry for tgid={tgid} has no usable time — using local clock.")
            started_at = now

        if self._active_call_id is None:
            await self._open_segment(entry, tgid, started_at, now)
            return

        if tgid == self._current_tgid:
            # CONTINUE: same talkgroup, keep one recording so the back-and-forth
            # of a single conversation lands in one file.
            self._transmissions += 1
            self._tx_active = True
            self._last_tx_end = None
            self._last_activity = now
            self._refresh_meta_from_log(entry)
            return

        # SPLIT: different talkgroup. The new grant's OP25 timestamp is the most
        # precise end available for the outgoing segment — the new call's audio
        # starts exactly there, so no tail pad.
        await self._close_segment(started_at, reason="tgid_change")
        await self._open_segment(entry, tgid, started_at, now)

    async def _handle_channels(self, channels: List[Dict[str, Any]], now: float) -> None:
        if self._active_call_id is None:
            return

        tx_active = False
        foreign_active_tgid: Optional[int] = None

        for channel in channels:
            srcaddr = _as_int(channel.get("srcaddr"))
            chan_tgid = _as_int(channel.get("tgid"))
            if srcaddr is None:
                continue
            if chan_tgid == self._current_tgid:
                tx_active = True
                self._current_srcaddr = srcaddr
                self._refresh_meta_from_channel(channel)
            elif chan_tgid is not None:
                foreign_active_tgid = chan_tgid

        if tx_active:
            self._tx_active = True
            self._last_tx_end = None
            self._last_activity = now
        elif self._tx_active:
            # The srcaddr != 0 → 0 edge: OP25 has torn the call down.
            self._tx_active = False
            self._last_tx_end = now
            self._last_activity = now

        # Safety net for a dropped call_log event (deque is capped at 10): the one
        # receiver we have is plainly on another talkgroup, so our segment is over
        # even though we never saw its grant. Close now rather than record
        # call_idle_timeout seconds of the wrong tgid.
        #
        # Restricted to single-receiver setups on purpose: with several receivers,
        # another channel being busy says nothing about ours, and closing on it
        # would truncate every call whenever a second receiver is active.
        if not tx_active and foreign_active_tgid is not None and len(channels) == 1:
            logger.warning(
                f"tgid {foreign_active_tgid} active without a call_log entry — "
                f"closing segment for tgid {self._current_tgid} (call_log event likely dropped)."
            )
            await self._close_segment(now, reason="tgid_change_unlogged")
            return

        if (now - self._last_activity) >= settings.call_idle_timeout:
            # STOP: quiet for long enough. End the audio at the last transmission
            # plus a short pad, not at "now" — otherwise every recording carries
            # call_idle_timeout seconds of silence.
            #
            # The measured idle below is the CONTROL-CHANNEL idle (srcaddr 1→0
            # edge → now). It is NOT comparable to silence measured in the audio,
            # which additionally contains the ~1.9 s P25 grant→speech delay.
            # Tune settings.call_idle_timeout from THIS number and nothing else.
            if self._last_tx_end is not None:
                measured_idle = now - self._last_tx_end
                end = self._last_tx_end + _tail_pad()
                logger.info(
                    f"Idle timeout for tgid {self._current_tgid}: measured control-channel idle "
                    f"{measured_idle:.2f}s (threshold {settings.call_idle_timeout:.2f}s, "
                    f"tail pad {_tail_pad():.2f}s)."
                )
            else:
                end = now
                logger.info(
                    f"Idle timeout for tgid {self._current_tgid}: no srcaddr end edge observed, "
                    f"idle {now - self._last_activity:.2f}s measured from last activity."
                )
            await self._close_segment(min(end, now), reason="idle_timeout")
            return

        if self._started_at is not None and (now - self._started_at) >= MAX_SEGMENT_SECONDS:
            logger.warning(f"Segment for tgid {self._current_tgid} hit the {MAX_SEGMENT_SECONDS}s cap — closing.")
            await self._close_segment(now, reason="max_length")

    # ------------------------------------------------------------------
    # Segment open / close
    # ------------------------------------------------------------------

    def _refresh_meta_from_log(self, entry: Dict[str, Any]) -> None:
        if not self._current_tgid_name:
            self._current_tgid_name = entry.get("tgtag") or ""
        if entry.get("freq"):
            self._current_freq = entry.get("freq")
        rid = _as_int(entry.get("rid"))
        if rid is not None:
            self._current_srcaddr = rid

    def _refresh_meta_from_channel(self, channel: Dict[str, Any]) -> None:
        if not self._current_tgid_name:
            self._current_tgid_name = channel.get("tag") or ""
        if not self._current_freq and channel.get("freq"):
            self._current_freq = channel.get("freq")

    async def _open_segment(self, entry: Dict[str, Any], tgid: int, started_at: float, now: float) -> None:
        self._active_call_id = str(uuid.uuid4())
        self._current_tgid = tgid
        self._current_tgid_name = entry.get("tgtag") or ""
        self._current_freq = entry.get("freq")
        self._current_srcaddr = _as_int(entry.get("rid"))
        self._started_at = started_at
        self._transmissions = 1

        # Assume the transmission is still up: we learn otherwise from the next
        # channel scan. A grant whose call already ended before we polled simply
        # closes on the very next tick via the idle timeout.
        self._tx_active = True
        self._last_tx_end = None
        self._last_activity = now

        payload = {
            "call_id": self._active_call_id,
            "tgid": tgid,
            "tgid_name": self._current_tgid_name,
            "freq": self._current_freq,
            "srcaddr": self._current_srcaddr,
            "started_at": _iso(started_at),
            # Raw epoch for the recorder's ring-buffer slice — same clock domain.
            "started_at_epoch": started_at,
        }
        logger.info(
            f"Call start: tgid={tgid} id={self._active_call_id} "
            f"(op25 t={started_at:.3f}, detected {now - started_at:+.2f}s later)"
        )
        if self.on_call_start:
            await self.on_call_start(payload)

    async def _close_segment(self, end_epoch: float, reason: str) -> None:
        if not self._active_call_id:
            return

        started_at = self._started_at
        if started_at is not None:
            end_epoch = max(end_epoch, started_at)

        payload = {
            "call_id": self._active_call_id,
            "tgid": self._current_tgid,
            "tgid_name": self._current_tgid_name or "",
            "freq": self._current_freq,
            "srcaddr": self._current_srcaddr,
            "started_at": _iso(started_at),
            "started_at_epoch": started_at,
            "ended_at": _iso(end_epoch),
            "ended_at_epoch": end_epoch,
            "transmissions": self._transmissions,
            "end_reason": reason,
        }
        duration = (end_epoch - started_at) if started_at is not None else 0.0
        logger.info(
            f"Call end: id={self._active_call_id} tgid={self._current_tgid} "
            f"reason={reason} transmissions={self._transmissions} duration={duration:.2f}s"
        )

        # Clear state before awaiting so a re-entrant tick can't see a half-closed
        # segment (and so an immediately-following _open_segment is clean).
        self._active_call_id = None
        self._current_tgid = None
        self._current_tgid_name = None
        self._current_freq = None
        self._current_srcaddr = None
        self._started_at = None
        self._transmissions = 0
        self._tx_active = False
        self._last_tx_end = None

        if self.on_call_end:
            await self.on_call_end(payload)

    # ------------------------------------------------------------------
    # Public state (consumed by routers/api.py, main.py and the dashboards)
    # ------------------------------------------------------------------

    @property
    def active_call_id(self) -> Optional[str]:
        return self._active_call_id

    @property
    def current_tgid(self) -> Optional[int]:
        return self._current_tgid

    @property
    def current_tgid_name(self) -> Optional[str]:
        return self._current_tgid_name

    @property
    def is_active(self) -> bool:
        return self._active_call_id is not None


metadata_watcher = MetadataWatcher()
