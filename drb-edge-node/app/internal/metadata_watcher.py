"""
Call segmentation: AUDIO decides the boundaries, the CONSOLE decides the label.

    START  first chunk of audio above the silence threshold (voice onset).
    STOP   settings.call_silence_timeout seconds of continuous silence HEARD in
           that audio.
    LABEL  talkgroup / alias / rid, resolved from OP25 console observations that
           fall inside the recording's window, resolved AT CLOSE TIME.
    SPLIT  a console talkgroup change still forces a cut, even mid-audio.

WHY THE CONTROL CHANNEL NO LONGER DECIDES BOUNDARIES. The previous design
started a segment on an OP25 `call_log` grant and ended it by inferring from the
control channel: the `srcaddr != 0 -> 0` edge started an idle timer and the
segment closed call_idle_timeout seconds later. Both halves were measured wrong
in the field:

  * The grant fires 0.84-1.62 s (variable) before anyone speaks, so a
    grant-anchored window is always guessing at the offset.
  * `srcaddr` can drop to 0 WHILE SOMEONE IS STILL TALKING. Measured across six
    recordings, five had healthy trailing silence trimmed (-0.53 s to -2.48 s)
    but one reported "-1.61s lead, -0.00s tail" — the trim found nothing to
    remove because the capture window had closed on top of live speech. The
    recording ends on an unfinished word. Working backwards from its lead trim,
    the audio pipeline lag was at most 1.36 s, so the window should have held
    ~1.6 s more; the only consistent explanation is a false early `srcaddr -> 0`.

Audio is the ground truth for WHEN. It cannot answer WHO, so the console is
still the only source of talkgroup, alias and radio id.

WHY ATTRIBUTION HAPPENS AT CLOSE, NOT AT OPEN. There is no guaranteed ordering
between a grant and the audio it belongs to: the console is polled every 500 ms
and the audio pipeline lag is variable, so the grant can land after voice onset
just as easily as before it. A segment may therefore open unattributed and
acquire its talkgroup part-way through, which is expected and fine. At close we
have seen the whole window and ask the rolling console history "what was active
during this audio, give or take a few seconds" — see _attribute and the
ATTRIBUTION_* constants.

CONVENTIONAL CHANNELS (node-26#24 P25 conventional, node-26#20 analog). There
is no control channel, so internal/conventional.py rewrites each console update
first: a conventional channel that is keyed becomes an ordinary active row whose
talkgroup is the link-control TGID when it is distinctive, else a stable
synthetic id derived from the channel frequency. Such a row counts as a talker
without a radio id (`drb_active`) — link control does not always carry one and
analog never does. One more rule, applied only at CLOSE: with exactly one
conventional channel configured, audio the console said nothing about can only
have come from that channel, so it is attributed to it rather than discarded.
Not at open: on a channel whose link control carries a real talkgroup, the
grant may arrive a poll after the audio, and a premature synthetic label would
force a bogus split when it does.

ORPHAN AUDIO. If nothing in the console history overlaps the window, the audio
is unattributed: Liquidsoap fallback, a test tone, stray noise, or a dropped
`call_log`. Policy is DISCARD AND SHOUT — the recording is not uploaded and no
call_start/call_end is published, because a call with no talkgroup silently
poisons incident correlation downstream, and that is worse than losing the
audio. It is logged at ERROR with the window and everything nearby that was
considered, and counted on /api/status so it cannot pass unnoticed.

FALLBACK MODE. When PulseAudio capture is NOT producing audio there is nothing
to segment on, so the old console state machine still runs (grant opens,
srcaddr edge + call_idle_timeout closes). It produces no audio — capture is
down — but it keeps the node reporting real radio activity to C2 while the
audio path is broken. This is the only remaining consumer of
settings.call_idle_timeout.

SEGMENTS: one emitted call (= one recording, one Firestore doc) spans a whole
conversation, not a single transmission. It stays open across repeated grants on
the same talkgroup and closes when the talkgroup changes or the AUDIO goes quiet
for settings.call_silence_timeout seconds.

CLOCKS: `call_log["time"]` is time.time() inside the op25 container. All three
client containers run network_mode: host and share the host kernel clock, so that
value is directly comparable to time.time() here — no offset mapping needed. The
call recorder's chunk timestamps are the same clock, with the caveat that they
are ARRIVAL times and therefore lag the moment of speech by the pipeline
latency. Comparisons between two audio timestamps are exact; comparisons between
audio and console timestamps carry that lag, which is what _tail_pad() covers.
"""
import asyncio
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Callable, Awaitable, Any, List, Dict

from app.config import settings
from app.internal.call_recorder import AudioActivity
from app.internal.conventional import ConventionalChannels
from app.internal.op25_client import op25_client
from app.internal.logger import logger

CallbackFn = Callable[[dict], Awaitable[None]]
ActivityFn = Callable[[], AudioActivity]

# 500 ms. Do NOT lower: audio boundaries come from the recorder's own chunk
# timestamps (~46 ms resolution), not from when this loop happens to notice
# them, and http_server.py's request handler has a ~200 ms blocking floor anyway.
POLL_INTERVAL = 0.5

# Seconds of unreachable OP25 before an open segment is force-closed. Applies in
# both modes: without the console there is no attribution, and unattributed
# audio is discarded anyway.
OP25_OFFLINE_GRACE = 3.0

# Hard ceiling on a single segment; mirrors MAX_RECORDING_SECONDS in call_recorder
# so a talkgroup that never goes quiet cannot produce an unbounded recording. In
# audio mode a new segment is opened immediately afterwards if voice is still
# present, so a genuinely long transmission is split rather than truncated.
MAX_SEGMENT_SECONDS = 600

# How far either side of the AUDIO window console observations are still
# accepted as attribution evidence. "Plus or minus some seconds", made explicit:
#
#   LOOKBACK  the grant normally PRECEDES the audio — 0.84-1.62 s of
#             grant-to-speech delay, plus up to ~1.4 s of audio pipeline lag,
#             plus one 0.5 s poll of detection slack. 4.0 s covers the worst
#             case measured with margin.
#   LOOKAHEAD the grant can also FOLLOW voice onset, because the console is only
#             polled every 500 ms and OP25 logs the grant on its own schedule.
#             2.0 s is four poll intervals.
#
# Both are deliberately asymmetric: the "grant first" direction is the common
# one and has the larger physical spread.
ATTRIBUTION_LOOKBACK_SECONDS = 4.0
ATTRIBUTION_LOOKAHEAD_SECONDS = 2.0

# Rolling console history. Bounded twice — by age and by entry count — so a busy
# system cannot grow it without limit. At ~2 observations per poll this is a few
# minutes of history for a few tens of KB.
CONSOLE_HISTORY_SECONDS = 180.0
CONSOLE_HISTORY_MAX = 1200

# How far back to look for a duplicate before appending a grant. OP25's call_log
# deque drains on read so repeats should not happen, but a re-delivered entry
# would otherwise inflate the transmission count and the attribution score.
_GRANT_DEDUPE_DEPTH = 24

# Close reasons where the console explicitly told us the talkgroup changed, so
# the segment's label is already known first-hand and close-time attribution
# would only be able to make it worse (the window extends past the split).
_SPLIT_REASONS = ("tgid_change", "tgid_change_unlogged")


def _tail_pad() -> float:
    """
    Audio kept past a CONSOLE-DERIVED segment boundary, to cover the fact that
    buffered audio lags control-channel timestamps.

    Under audio-driven segmentation this no longer applies to the normal end of
    a call — that boundary now comes from the audio itself and needs no pad. It
    still applies wherever a boundary is a control-channel timestamp:

        tgid_change            close at the new grant's timestamp + pad
        tgid_change_unlogged   close at the observing poll's timestamp + pad
        idle_timeout           console fallback mode only

    Read live from settings (env CALL_TAIL_PAD_SECONDS) rather than frozen into
    a module constant, so it is tunable per node.

    An earlier version of this docstring claimed the tgid_change paths close at
    "an exact, already-known boundary" and so intentionally added no pad — THAT
    REASONING WAS WRONG and produced real truncated recordings. The boundary is
    exact only in CONTROL-CHANNEL time; the buffered AUDIO lags control-channel
    timestamps by ~1.5 s (measured: 0.84-1.62 s of lead trimmed across 7 field
    calls), so slicing the outgoing call at the new grant's exact timestamp cut
    roughly the last 1.5 s of its real speech. Do not reintroduce a zero-pad
    close for tgid_change or tgid_change_unlogged; if the outgoing and incoming
    recordings end up overlapping in the underlying audio because of this pad,
    that is correct — the audio genuinely contains both.
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


def _talker(channel: Dict[str, Any]) -> Optional[tuple]:
    """(tgid, rid) if this channel row says someone is transmitting, else None.

    Trunked rows need both a talkgroup and a source address — an idle channel
    can keep its last tgid. A normalized conventional row (`drb_active`) only
    has a tgid while a call is up, and may legitimately have no radio id.
    """
    tgid = _as_int(channel.get("tgid"))
    if tgid is None:
        return None
    rid = _as_int(channel.get("srcaddr"))
    if rid is None and not channel.get("drb_active"):
        return None
    return tgid, rid


def _iso(epoch: Optional[float]) -> Optional[str]:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


@dataclass(frozen=True)
class ConsoleEvent:
    """One thing the OP25 console said, kept so a closing segment can ask about it."""

    epoch: float
    tgid: int
    name: str = ""
    freq: Any = None
    rid: Optional[int] = None
    # True for a `call_log` grant, False for an active `channel_update` row.
    is_grant: bool = False


@dataclass
class Attribution:
    """Who a stretch of audio belonged to, and how confident we are."""

    tgid: int
    name: str = ""
    freq: Any = None
    rid: Optional[int] = None
    grants: int = 0
    # Observations that fall strictly inside the audio window (vs only inside
    # the tolerance band around it).
    overlap: int = 0
    nearby: int = 0
    competing: List[int] = field(default_factory=list)


class MetadataWatcher:
    def __init__(self):
        self._running = False

        # Open segment state
        self._active_call_id: Optional[str] = None
        self._current_tgid: Optional[int] = None
        self._current_tgid_name: Optional[str] = None
        self._current_freq: Any = None
        self._current_srcaddr: Optional[int] = None
        self._started_at: Optional[float] = None   # audio onset, or grant epoch in fallback mode
        self._transmissions: int = 0
        # True when the open segment is governed by audio, False for the
        # console fallback. Fixed at open so capture flapping cannot switch the
        # rules underneath a live segment.
        self._audio_driven: bool = False

        # Transmission tracking within the open segment (console fallback mode)
        self._tx_active: bool = False              # last poll saw srcaddr != 0
        self._last_activity: float = 0.0           # epoch of last evidence of traffic
        self._last_tx_end: Optional[float] = None  # epoch of the srcaddr 1→0 edge
        self._last_ok_poll: float = 0.0

        # Dashboard-only view of what the OP25 console last said. Read by
        # /api/health; never consulted by segmentation or attribution.
        self._terminal_epoch: Optional[float] = None
        self._terminal_channels: List[Dict[str, Any]] = []
        self._terminal_trunk: List[Dict[str, Any]] = []
        self._last_heard: Optional[ConsoleEvent] = None

        # Rolling console history for close-time attribution.
        self._console: deque[ConsoleEvent] = deque(maxlen=CONSOLE_HISTORY_MAX)

        # Onset of the voice run the last audio-driven segment covered, so the
        # same run cannot immediately re-open a second segment.
        self._consumed_onset: Optional[float] = None

        # Field-visible counter of discarded orphan audio.
        self._unattributed_segments: int = 0

        # Conventional-channel normalizer (node-26#24/#20); also remembers the
        # channel list and their encryption state for the dashboard + checkin.
        self._conventional = ConventionalChannels()

        # Injectable for tests; production is always the host wall clock.
        self._clock: Callable[[], float] = time.time

        # Set these before calling start()
        self.on_call_start: Optional[CallbackFn] = None
        self.on_call_end: Optional[CallbackFn] = None
        # Supplies the audio-activity snapshot. None (or a snapshot reporting
        # capturing=False) puts the watcher in console fallback mode.
        self.audio_activity: Optional[ActivityFn] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        self._running = True
        self._last_ok_poll = self._clock()
        asyncio.create_task(self._poll_loop())
        logger.info("Metadata watcher started (audio-driven segmentation, console attribution).")

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
        update = self._conventional.normalize(update)
        self._record_console(update, now)
        self._note_terminal(update, now)

        activity = self._snapshot()
        if activity is None or not activity.capturing:
            await self._console_tick(update, now)
            return

        await self._audio_tick(update, activity, now)

    def _snapshot(self) -> Optional[AudioActivity]:
        if self.audio_activity is None:
            return None
        try:
            return self.audio_activity()
        except Exception as e:
            logger.warning(f"Audio activity unavailable ({e}) — falling back to console segmentation.")
            return None

    # ------------------------------------------------------------------
    # Console history (feeds close-time attribution)
    # ------------------------------------------------------------------

    def _record_console(self, update: Any, now: float) -> None:
        for entry in update.call_log:
            tgid = _as_int(entry.get("tgid"))
            if tgid is None:
                continue
            epoch = _as_float(entry.get("time"))
            event = ConsoleEvent(
                epoch=now if epoch is None else epoch,
                tgid=tgid,
                name=entry.get("tgtag") or "",
                freq=entry.get("freq"),
                rid=_as_int(entry.get("rid")),
                is_grant=True,
            )
            if not self._is_duplicate_grant(event):
                self._console.append(event)

        for channel in update.channels:
            talker = _talker(channel)
            if talker is None:
                continue  # idle channel says nothing about who is talking
            tgid, srcaddr = talker
            self._console.append(ConsoleEvent(
                epoch=now,
                tgid=tgid,
                name=channel.get("tag") or "",
                freq=channel.get("freq"),
                rid=srcaddr,
                is_grant=False,
            ))

        cutoff = now - CONSOLE_HISTORY_SECONDS
        while self._console and self._console[0].epoch < cutoff:
            self._console.popleft()

    def _note_terminal(self, update: Any, now: float) -> None:
        """Keep the latest console snapshot for the dashboard. No side effects
        on segment state — trunk_update/channel_update are not sent on every
        poll, so the last non-empty one is kept."""
        self._terminal_epoch = now
        if update.channels:
            self._terminal_channels = update.channels
        if getattr(update, "trunk", None):
            self._terminal_trunk = update.trunk
        if self._console:
            self._last_heard = self._console[-1]

    def _is_duplicate_grant(self, event: ConsoleEvent) -> bool:
        for index in range(len(self._console) - 1, -1, -1):
            if len(self._console) - index > _GRANT_DEDUPE_DEPTH:
                return False
            known = self._console[index]
            if known.is_grant and known.tgid == event.tgid and known.epoch == event.epoch:
                return True
        return False

    def _attribute(self, start: float, end: float, allow_sole_channel: bool = False) -> Optional[Attribution]:
        """
        Resolve which talkgroup a stretch of audio belongs to.

        Scores every talkgroup seen in [start - LOOKBACK, end + LOOKAHEAD] by
        how well its console activity overlaps the audio itself, preferring
        real overlap over merely being nearby, and grants over channel rows.
        Returns None only when NOTHING was observed in that band at all — the
        orphan-audio case — unless allow_sole_channel and the node listens to
        exactly one conventional channel (see module docstring).
        """
        low = start - ATTRIBUTION_LOOKBACK_SECONDS
        high = end + ATTRIBUTION_LOOKAHEAD_SECONDS
        candidates: Dict[int, Attribution] = {}
        firsts: Dict[int, float] = {}

        for event in self._console:
            if event.epoch < low or event.epoch > high:
                continue
            found = candidates.get(event.tgid)
            if found is None:
                found = Attribution(tgid=event.tgid)
                candidates[event.tgid] = found
                firsts[event.tgid] = event.epoch
            found.nearby += 1
            if start <= event.epoch <= end:
                found.overlap += 1
            if event.is_grant:
                found.grants += 1
            if event.name and not found.name:
                found.name = event.name
            if event.freq and found.freq is None:
                found.freq = event.freq
            if event.rid is not None:
                found.rid = event.rid  # most recent wins

        if not candidates:
            sole = self._conventional.sole_channel() if allow_sole_channel else None
            if sole is None:
                return None
            logger.info(
                f"No console activity near {start:.3f}-{end:.3f}; attributing to the only "
                f"conventional channel, {sole['name']} (tgid {sole['tgid']})."
            )
            return Attribution(tgid=sole["tgid"], name=sole["name"], freq=sole["freq"])

        best = max(
            candidates.values(),
            key=lambda a: (a.overlap, a.grants, a.nearby, -firsts[a.tgid]),
        )
        best.competing = sorted(
            tgid for tgid, a in candidates.items() if tgid != best.tgid and a.overlap > 0
        )
        return best

    # ------------------------------------------------------------------
    # Audio-driven segmentation
    # ------------------------------------------------------------------

    async def _audio_tick(self, update: Any, activity: AudioActivity, now: float) -> None:
        if self._active_call_id is not None and not self._audio_driven:
            # A segment that opened while capture was down finishes under the
            # rules it started with rather than switching mid-flight.
            await self._console_tick(update, now)
            return

        # 1. Console first: a talkgroup change must still force a split even
        #    when the audio never went quiet, and a grant may be the thing that
        #    finally attributes an already-open segment.
        for entry in sorted(update.call_log, key=lambda e: _as_float(e.get("time")) or 0.0):
            await self._handle_grant(entry, now)
        await self._scan_channels(update.channels, now)

        # 2. Then the audio decides the boundaries.
        last_voice = activity.last_voice_epoch
        voice_active = last_voice is not None and (now - last_voice) < settings.call_silence_timeout

        if self._active_call_id is None:
            onset = activity.voice_onset_epoch
            if voice_active and onset is not None and (
                self._consumed_onset is None or onset > self._consumed_onset
            ):
                await self._open_from_audio(onset, now)
            return

        if not voice_active:
            silence = (now - last_voice) if last_voice is not None else settings.call_silence_timeout
            # The measured trailing silence, in the AUDIO's own clock. This is
            # the number to tune settings.call_silence_timeout from — unlike the
            # old control-channel idle it contains no grant-to-speech delay, so
            # it means exactly what it says.
            logger.info(
                f"Audio silence close for tgid {self._current_tgid}: measured trailing silence "
                f"{silence:.2f}s (threshold {settings.call_silence_timeout:.2f}s at "
                f"{settings.call_silence_threshold_db:.1f}dBFS)."
            )
            self._consumed_onset = activity.voice_onset_epoch
            end = (last_voice + settings.call_silence_timeout) if last_voice is not None else now
            await self._close_segment(min(end, now), reason="audio_silence")
            return

        if self._started_at is not None and (now - self._started_at) >= MAX_SEGMENT_SECONDS:
            logger.warning(
                f"Segment for tgid {self._current_tgid} hit the {MAX_SEGMENT_SECONDS}s cap while audio "
                "was still live — closing and immediately reopening so nothing is dropped. If this "
                "repeats, the silence threshold may be low enough that noise reads as voice."
            )
            await self._close_segment(now, reason="max_length")
            await self._open_from_audio(now, now)

    async def _open_from_audio(self, onset: float, now: float) -> None:
        """Open a segment at a detected voice onset, attributing it if we can."""
        found = self._attribute(onset, now)
        await self._open_segment(
            started_at=onset,
            now=now,
            tgid=found.tgid if found else None,
            tgid_name=found.name if found else "",
            freq=found.freq if found else None,
            srcaddr=found.rid if found else None,
            audio_driven=True,
            transmissions=found.grants if found else 0,
        )

    async def _handle_grant(self, entry: Dict[str, Any], now: float) -> None:
        """A `call_log` grant, interpreted in audio mode: label or split, never start."""
        tgid = _as_int(entry.get("tgid"))
        if tgid is None:
            return  # a grant with no talkgroup is nothing we can label with

        started_at = _as_float(entry.get("time"))
        if started_at is None:
            logger.warning(f"call_log entry for tgid={tgid} has no usable time — using local clock.")
            started_at = now

        if self._active_call_id is None:
            # Audio starts recordings, not grants. The grant is already in the
            # console history and will attribute the segment when audio arrives.
            return

        if self._current_tgid is None:
            self._current_tgid = tgid
            self._current_tgid_name = entry.get("tgtag") or ""
            self._current_freq = entry.get("freq")
            self._current_srcaddr = _as_int(entry.get("rid"))
            self._transmissions += 1
            logger.info(
                f"Late attribution: segment {self._active_call_id} adopted tgid {tgid} from a grant "
                f"logged {started_at - (self._started_at or started_at):+.2f}s from audio onset."
            )
            return

        if tgid == self._current_tgid:
            # CONTINUE: same talkgroup, keep one recording so the back-and-forth
            # of a single conversation lands in one file.
            self._transmissions += 1
            self._refresh_meta_from_log(entry)
            return

        if self._same_channel(tgid, entry.get("freq"), entry.get("tgtag") or ""):
            self._transmissions += 1
            self._refresh_meta_from_log(entry)
            return

        # FORCED SPLIT. Two talkgroups can be back to back with no silence
        # between them; pure audio segmentation would merge them into one file
        # under one label, which is exactly the kind of wrong that corrupts
        # incident correlation. The console change is authoritative here.
        await self._close_segment(started_at + _tail_pad(), reason="tgid_change")
        await self._open_segment(
            started_at=started_at,
            now=now,
            tgid=tgid,
            tgid_name=entry.get("tgtag") or "",
            freq=entry.get("freq"),
            srcaddr=_as_int(entry.get("rid")),
            audio_driven=True,
            transmissions=1,
        )

    def _same_channel(self, tgid: int, freq: Any, name: str) -> bool:
        """A conventional channel seen under its synthetic id and its real
        talkgroup is one channel, not a split (see ConventionalChannels.
        same_channel_relabel). Adopts the real talkgroup when offered."""
        decision = self._conventional.same_channel_relabel(self._current_tgid, self._current_freq, tgid, freq)
        if decision == "adopt":
            logger.info(
                f"Segment {self._active_call_id}: link control names tgid {tgid} for the channel "
                f"labelled {self._current_tgid} — relabelling, not splitting."
            )
            self._current_tgid = tgid
            if name:
                self._current_tgid_name = name
        return decision is not None

    async def _scan_channels(self, channels: List[Dict[str, Any]], now: float) -> None:
        """Channel rows in audio mode: refresh metadata, catch an unlogged split."""
        if self._active_call_id is None:
            return

        active: List[Dict[str, Any]] = []
        ours = False
        for channel in channels:
            talker = _talker(channel)
            if talker is None:
                continue
            tgid, srcaddr = talker
            active.append(channel)
            if tgid == self._current_tgid or self._same_channel(tgid, channel.get("freq"), channel.get("tag") or ""):
                ours = True
                if srcaddr is not None:
                    self._current_srcaddr = srcaddr
                self._last_activity = now
                self._refresh_meta_from_channel(channel)

        if self._current_tgid is None:
            # Late attribution from a channel row — this is the path that saves
            # us when the grant itself was dropped from OP25's capped deque.
            if len(active) == 1:
                tgid = _as_int(active[0].get("tgid"))
                self._current_tgid = tgid
                self._current_tgid_name = active[0].get("tag") or ""
                self._current_freq = active[0].get("freq")
                self._current_srcaddr = _as_int(active[0].get("srcaddr"))
                logger.info(f"Late attribution: segment {self._active_call_id} adopted tgid {tgid} from channel state.")
            return

        if ours or not active or len(channels) != 1:
            # Restricted to single-receiver setups on purpose: with several
            # receivers, another channel being busy says nothing about ours.
            return

        foreign = _as_int(active[0].get("tgid"))
        if foreign is None or foreign == self._current_tgid:
            return

        logger.warning(
            f"tgid {foreign} active without a call_log entry — splitting segment for tgid "
            f"{self._current_tgid} (call_log event likely dropped)."
        )
        await self._close_segment(now + _tail_pad(), reason="tgid_change_unlogged")
        await self._open_segment(
            started_at=now,
            now=now,
            tgid=foreign,
            tgid_name=active[0].get("tag") or "",
            freq=active[0].get("freq"),
            srcaddr=_as_int(active[0].get("srcaddr")),
            audio_driven=True,
            transmissions=1,
        )

    # ------------------------------------------------------------------
    # Console fallback segmentation (capture down)
    # ------------------------------------------------------------------

    async def _console_tick(self, update: Any, now: float) -> None:
        if self._active_call_id is not None and self._audio_driven:
            logger.warning(
                f"PulseAudio capture stopped while recording {self._active_call_id} — closing the "
                "segment at the last captured audio; segmentation falls back to the control channel."
            )
            await self._close_segment(now, reason="capture_lost")
            return

        # 1. call_log first — these are the authoritative starts, and processing
        #    them before the channel scan means a same-poll grant+state pair is
        #    already attributed to the new segment by the time we scan channels.
        #    Sorted defensively: multi-receiver setups append per receiver.
        for entry in sorted(update.call_log, key=lambda e: _as_float(e.get("time")) or 0.0):
            await self._handle_call_log(entry, now)

        # 2. channel_update — the only external end signal available here.
        await self._handle_channels(update.channels, now)

    async def _handle_call_log(self, entry: Dict[str, Any], now: float) -> None:
        tgid = _as_int(entry.get("tgid"))
        if tgid is None:
            return

        started_at = _as_float(entry.get("time"))
        if started_at is None:
            logger.warning(f"call_log entry for tgid={tgid} has no usable time — using local clock.")
            started_at = now

        if self._active_call_id is None:
            await self._open_from_console(entry, tgid, started_at, now)
            return

        if tgid == self._current_tgid:
            self._transmissions += 1
            self._tx_active = True
            self._last_tx_end = None
            self._last_activity = now
            self._refresh_meta_from_log(entry)
            return

        await self._close_segment(started_at + _tail_pad(), reason="tgid_change")
        await self._open_from_console(entry, tgid, started_at, now)

    async def _handle_channels(self, channels: List[Dict[str, Any]], now: float) -> None:
        if self._active_call_id is None:
            return

        tx_active = False
        foreign_active_tgid: Optional[int] = None

        for channel in channels:
            talker = _talker(channel)
            if talker is None:
                continue
            chan_tgid, srcaddr = talker
            if chan_tgid == self._current_tgid:
                tx_active = True
                if srcaddr is not None:
                    self._current_srcaddr = srcaddr
                self._refresh_meta_from_channel(channel)
            elif chan_tgid is not None:
                foreign_active_tgid = chan_tgid

        if tx_active:
            self._tx_active = True
            self._last_tx_end = None
            self._last_activity = now
        elif self._tx_active:
            # The srcaddr != 0 → 0 edge. Note this is NOT trusted as an end of
            # speech any more (it fires mid-word in the field) — in fallback
            # mode there is simply nothing better available.
            self._tx_active = False
            self._last_tx_end = now
            self._last_activity = now

        if not tx_active and foreign_active_tgid is not None and len(channels) == 1:
            logger.warning(
                f"tgid {foreign_active_tgid} active without a call_log entry — "
                f"closing segment for tgid {self._current_tgid} (call_log event likely dropped)."
            )
            await self._close_segment(now + _tail_pad(), reason="tgid_change_unlogged")
            return

        if (now - self._last_activity) >= settings.call_idle_timeout:
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

    async def _open_from_console(self, entry: Dict[str, Any], tgid: int, started_at: float, now: float) -> None:
        await self._open_segment(
            started_at=started_at,
            now=now,
            tgid=tgid,
            tgid_name=entry.get("tgtag") or "",
            freq=entry.get("freq"),
            srcaddr=_as_int(entry.get("rid")),
            audio_driven=False,
            transmissions=1,
        )

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

    async def _open_segment(
        self,
        started_at: float,
        now: float,
        tgid: Optional[int],
        tgid_name: str,
        freq: Any,
        srcaddr: Optional[int],
        audio_driven: bool,
        transmissions: int = 1,
    ) -> None:
        self._active_call_id = str(uuid.uuid4())
        self._current_tgid = tgid
        self._current_tgid_name = tgid_name
        self._current_freq = freq
        self._current_srcaddr = srcaddr
        self._started_at = started_at
        self._transmissions = max(1, transmissions)
        self._audio_driven = audio_driven

        # Console fallback assumes the transmission is still up; it learns
        # otherwise from the next channel scan.
        self._tx_active = not audio_driven
        self._last_tx_end = None
        self._last_activity = now

        payload = {
            "call_id": self._active_call_id,
            "tgid": tgid,
            "tgid_name": tgid_name,
            "freq": freq,
            "srcaddr": srcaddr,
            "started_at": _iso(started_at),
            # Raw epoch for the recorder's ring-buffer slice — same clock domain.
            "started_at_epoch": started_at,
            "attributed": tgid is not None,
            "driver": "audio" if audio_driven else "console",
        }
        source = "audio onset" if audio_driven else "op25 grant"
        logger.info(
            f"Call start: tgid={tgid} id={self._active_call_id} "
            f"({source} t={started_at:.3f}, detected {now - started_at:+.2f}s later)"
        )
        if self.on_call_start:
            await self.on_call_start(payload)

    async def _close_segment(self, end_epoch: float, reason: str) -> None:
        if not self._active_call_id:
            return

        started_at = self._started_at
        if started_at is not None:
            end_epoch = max(end_epoch, started_at)

        if self._audio_driven and reason not in _SPLIT_REASONS:
            self._resolve_attribution(started_at if started_at is not None else end_epoch, end_epoch)

        attributed = self._current_tgid is not None
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
            "attributed": attributed,
            "driver": "audio" if self._audio_driven else "console",
        }
        duration = (end_epoch - started_at) if started_at is not None else 0.0

        if not attributed:
            self._unattributed_segments += 1
            window_start = started_at if started_at is not None else end_epoch
            logger.error(
                f"ORPHAN AUDIO: {duration:.2f}s of audio ({self._active_call_id}, reason={reason}, "
                f"window {window_start:.3f}-{end_epoch:.3f}) had NO OP25 talkgroup anywhere within "
                f"{ATTRIBUTION_LOOKBACK_SECONDS:.0f}s before or {ATTRIBUTION_LOOKAHEAD_SECONDS:.0f}s "
                f"after it. It will be DISCARDED, not uploaded — an untagged call would poison "
                f"incident correlation. Causes: Liquidsoap fallback/test audio on drb_sink, OP25 not "
                f"decoding the control channel, or a dropped call_log. Console history holds "
                f"{len(self._console)} recent observations; total orphans this run: "
                f"{self._unattributed_segments}."
            )
        else:
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
        self._audio_driven = False

        if self.on_call_end:
            await self.on_call_end(payload)

    def _resolve_attribution(self, start: float, end: float) -> None:
        """
        Last chance to label an audio-driven segment, run at close.

        Only ADOPTS a talkgroup when the segment still has none. A tgid we
        already hold came from a grant or a channel row — the console stating
        outright who was transmitting — and an inference over a window is not
        allowed to overrule a direct statement. This matters because the window
        deliberately extends past the audio (ATTRIBUTION_LOOKAHEAD_SECONDS, and
        the tail pad on a split), so a neighbouring call's console activity can
        legitimately fall inside it.

        A disagreement is still worth knowing about, so it is logged: it means
        two talkgroups' console activity overlaps one recording, i.e. the split
        logic should have fired and did not.
        """
        found = self._attribute(start, end, allow_sole_channel=self._current_tgid is None)
        if found is None:
            return

        if self._current_tgid is None:
            logger.info(
                f"Attributed {self._active_call_id} at close to tgid {found.tgid} "
                f"(overlap {found.overlap}, grants {found.grants}, nearby {found.nearby})."
            )
            self._current_tgid = found.tgid
            if found.name:
                self._current_tgid_name = found.name
            if found.freq is not None and not self._current_freq:
                self._current_freq = found.freq
            if found.rid is not None and self._current_srcaddr is None:
                self._current_srcaddr = found.rid
            self._transmissions = max(self._transmissions, found.grants)
            return

        others = sorted(set(found.competing) | ({found.tgid} if found.tgid != self._current_tgid else set()))
        others = [tgid for tgid in others if tgid != self._current_tgid]
        if others:
            logger.warning(
                f"Segment {self._active_call_id} (tgid {self._current_tgid}) overlaps console "
                f"activity for {others} as well — the split logic should have fired and did not. "
                "Keeping the talkgroup the console stated directly."
            )
        if not self._current_tgid_name and found.tgid == self._current_tgid and found.name:
            self._current_tgid_name = found.name

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

    @property
    def console_state(self) -> Dict[str, Any]:
        """Last thing the OP25 console reported, for /api/health only."""
        heard = self._last_heard
        return {
            "last_poll_epoch": self._terminal_epoch,
            "channels": self._terminal_channels,
            "trunk": self._terminal_trunk,
            "last_heard": None if heard is None else {
                "epoch": heard.epoch, "tgid": heard.tgid, "name": heard.name, "freq": heard.freq,
            },
        }

    @property
    def conventional_channels(self) -> List[Dict[str, Any]]:
        """Per conventional channel: name, freq, NAC heard, encryption. Empty on
        a trunked system. Read by /api/health and the checkin."""
        return self._conventional.encryption_report()

    @property
    def unattributed_segments(self) -> int:
        """Orphan-audio segments discarded since start. Surfaced on /api/status."""
        return self._unattributed_segments


metadata_watcher = MetadataWatcher()
