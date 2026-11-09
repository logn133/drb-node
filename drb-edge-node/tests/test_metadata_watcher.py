"""
Unit tests for the MetadataWatcher segmentation state machine.

TWO MODES, both covered here:

  AUDIO MODE (production) — a recording STARTS at voice onset heard in the
  captured audio and STOPS after settings.call_silence_timeout seconds of
  silence heard in the same audio. The OP25 console supplies only the LABEL
  (talkgroup/alias/rid), resolved at CLOSE time from a rolling history, and the
  forced SPLIT when the talkgroup changes with no silence between calls.

  CONSOLE FALLBACK (capture down) — the older state machine: `call_log` grants
  start segments, the srcaddr != 0 -> 0 edge plus call_idle_timeout ends them.
  Tests that wire no audio provider exercise this path, which is exactly the
  behaviour a node falls back to when PulseAudio is not producing audio.

All OP25 HTTP calls are mocked — no running services required.
"""
import pytest
from unittest.mock import AsyncMock, patch

from app.config import settings
from app.internal.call_recorder import AudioActivity
from app.internal.metadata_watcher import (
    ATTRIBUTION_LOOKAHEAD_SECONDS,
    ATTRIBUTION_LOOKBACK_SECONDS,
    MAX_SEGMENT_SECONDS,
    MetadataWatcher,
    OP25_OFFLINE_GRACE,
    rebase_radios,
)
from app.internal.op25_client import TerminalUpdate, parse_terminal_messages


class FakeClock:
    """Manually advanced clock so idle timeouts are testable without sleeping."""

    def __init__(self, start: float = 1_700_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def watcher(clock):
    """Console fallback mode: no audio provider wired, capture assumed down."""
    w = MetadataWatcher()
    w._clock = clock
    w.on_call_start = AsyncMock()
    w.on_call_end = AsyncMock()
    return w


class FakeAudio:
    """
    Stands in for CallRecorder.audio_activity.

    Mirrors the recorder's own rule for what starts a new voice RUN: a
    non-silent chunk more than settings.call_silence_timeout after the previous
    one. Tests drive it with speak()/quiet() instead of synthesising PCM, so the
    segmentation logic is tested independently of the detector.
    """

    def __init__(self, clock):
        self.clock = clock
        self.capturing = True
        self.recording = False
        self.last_voice = None
        self.onset = None

    def speak(self, at=None):
        """Mark voice heard now (or at `at`)."""
        moment = self.clock.now if at is None else at
        if self.last_voice is None or (moment - self.last_voice) >= settings.call_silence_timeout:
            self.onset = moment
        self.last_voice = moment
        return moment

    def __call__(self) -> AudioActivity:
        silence = 0.0 if self.last_voice is None else max(0.0, self.clock.now - self.last_voice)
        return AudioActivity(
            capturing=self.capturing,
            recording=self.recording,
            last_voice_epoch=self.last_voice,
            voice_onset_epoch=self.onset,
            silence_seconds=silence,
        )


@pytest.fixture
def audio(clock):
    return FakeAudio(clock)


@pytest.fixture
def hearing(clock, audio):
    """Audio mode: the production wiring, with a controllable audio stream."""
    w = MetadataWatcher()
    w._clock = clock
    w.on_call_start = AsyncMock()
    w.on_call_end = AsyncMock()
    w.audio_activity = audio
    return w


def grant(tgid: int, time_: float, tgtag: str = "", rid: int = 101, freq: int = 851_000_000):
    """One OP25 call_log entry (see tk_p25.log_call)."""
    return {
        "time": time_,
        "sysid": 1,
        "rcvr": 0,
        "freq": freq,
        "slot": None,
        "prio": 0,
        "tgid": tgid,
        "tgtag": tgtag,
        "rid": rid,
        "rtag": "",
    }


def channel(tgid: int = 0, srcaddr: int = 0, tag: str = "", hold_tgid: int = 0):
    """One OP25 channel_update channel dict (see tk_p25.get_chan_status)."""
    return {
        "freq": 851_000_000,
        "tgid": tgid or None,
        "tag": tag,
        "srcaddr": srcaddr,
        "svcopts": bool(srcaddr),
        "hold_tgid": hold_tgid or None,
        "encrypted": 0,
        "emergency": 0,
    }


def update(call_log=None, channels=None) -> TerminalUpdate:
    return TerminalUpdate(channels=list(channels or []), call_log=list(call_log or []))


def patched(result):
    return patch(
        "app.internal.metadata_watcher.op25_client.poll_terminal",
        new=AsyncMock(return_value=result),
    )


async def tick(watcher, result):
    with patched(result):
        await watcher._tick()


# ---------------------------------------------------------------------------
# op25_client message parsing
# ---------------------------------------------------------------------------

def test_parser_extracts_call_log_and_channels():
    messages = [
        {"json_type": "trunk_update", "0": {"whatever": 1}},
        {"json_type": "channel_update", "channels": [0], "0": channel(tgid=1234, srcaddr=555)},
        {"json_type": "call_log", "log": [grant(1234, 100.0)]},
    ]
    result = parse_terminal_messages(messages)

    assert len(result.channels) == 1
    assert result.channels[0]["tgid"] == 1234
    assert len(result.call_log) == 1
    assert result.call_log[0]["time"] == 100.0


def test_parser_tolerates_garbage():
    """Unknown json_types, bare dicts and malformed entries must not raise."""
    assert parse_terminal_messages(None).call_log == []
    assert parse_terminal_messages("nope").channels == []
    assert parse_terminal_messages([None, 5, {"json_type": "mystery"}]).channels == []

    # A bare dict instead of a list.
    single = parse_terminal_messages({"json_type": "call_log", "log": [grant(1, 1.0), "junk"]})
    assert len(single.call_log) == 1

    # channel_update naming a channel that isn't present.
    missing = parse_terminal_messages([{"json_type": "channel_update", "channels": [0, 1], "0": channel(9)}])
    assert len(missing.channels) == 1


def test_parser_handles_multiple_channels():
    messages = [{
        "json_type": "channel_update",
        "channels": [0, 1],
        "0": channel(tgid=1111, srcaddr=1),
        "1": channel(tgid=2222, srcaddr=2),
    }]
    result = parse_terminal_messages(messages)
    assert [c["tgid"] for c in result.channels] == [1111, 2222]


# ---------------------------------------------------------------------------
# Call start — driven by call_log
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_call_log_entry_starts_call(watcher, clock):
    grant_time = clock.now - 0.4  # OP25 logged it before our poll noticed
    await tick(watcher, update(
        call_log=[grant(1234, grant_time, tgtag="Police Dispatch")],
        channels=[channel(tgid=1234, srcaddr=555)],
    ))

    assert watcher.is_active
    assert watcher.current_tgid == 1234
    watcher.on_call_start.assert_called_once()

    payload = watcher.on_call_start.call_args[0][0]
    assert payload["tgid"] == 1234
    assert payload["tgid_name"] == "Police Dispatch"
    assert payload["call_id"]
    # The whole point: the start is OP25's timestamp, not our detection time.
    assert payload["started_at_epoch"] == grant_time
    assert payload["started_at"].startswith("20")


@pytest.mark.asyncio
async def test_channel_activity_alone_does_not_start_a_call(watcher):
    """No call_log entry => no call, even with a live tgid on the channel."""
    await tick(watcher, update(channels=[channel(tgid=1234, srcaddr=555)]))

    assert not watcher.is_active
    watcher.on_call_start.assert_not_called()


@pytest.mark.asyncio
async def test_call_log_entry_without_tgid_is_ignored(watcher):
    await tick(watcher, update(call_log=[grant(0, 100.0)]))

    assert not watcher.is_active
    watcher.on_call_start.assert_not_called()


@pytest.mark.asyncio
async def test_call_log_without_time_falls_back_to_local_clock(watcher, clock):
    entry = grant(1234, 0.0)
    entry.pop("time")
    await tick(watcher, update(call_log=[entry]))

    assert watcher.is_active
    assert watcher.on_call_start.call_args[0][0]["started_at_epoch"] == clock.now


@pytest.mark.asyncio
async def test_op25_unreachable_does_not_start_call(watcher):
    await tick(watcher, None)

    assert not watcher.is_active
    watcher.on_call_start.assert_not_called()


# ---------------------------------------------------------------------------
# Console fallback: call end — srcaddr edge + idle timeout
#
# This is the path a node uses ONLY when PulseAudio capture is not producing
# audio. It is measurably wrong (the srcaddr edge fires mid-word) but it is all
# there is when there is no audio to segment on, and it keeps the node
# reporting radio activity to C2 while the audio path is broken.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_srcaddr_edge_then_idle_timeout_ends_call(watcher, clock):
    await tick(watcher, update(
        call_log=[grant(1234, clock.now)],
        channels=[channel(tgid=1234, srcaddr=555)],
    ))
    assert watcher.is_active

    # srcaddr resets to 0 while tgid is still held — OP25's end-of-call signal.
    clock.advance(0.5)
    edge_time = clock.now
    await tick(watcher, update(channels=[channel(tgid=1234, srcaddr=0, hold_tgid=1234)]))
    assert watcher.is_active, "the srcaddr edge alone must not close the segment"
    watcher.on_call_end.assert_not_called()

    # Still quiet, but not long enough yet.
    clock.advance(settings.call_idle_timeout - 0.5)
    await tick(watcher, update(channels=[channel(tgid=1234, srcaddr=0, hold_tgid=1234)]))
    assert watcher.is_active

    # Past the timeout.
    clock.advance(1.0)
    await tick(watcher, update(channels=[channel()]))
    assert not watcher.is_active

    watcher.on_call_end.assert_called_once()
    payload = watcher.on_call_end.call_args[0][0]
    assert payload["end_reason"] == "idle_timeout"
    # The audio ends at the last transmission plus a short pad, NOT at "now" —
    # otherwise every recording carries call_idle_timeout seconds of silence.
    assert payload["ended_at_epoch"] == pytest.approx(edge_time + settings.call_tail_pad_seconds)
    assert payload["tgid"] == 1234


@pytest.mark.asyncio
async def test_tail_pad_is_configurable_and_defaults_to_three_seconds(watcher, clock, monkeypatch):
    """
    Field measurement showed the grant->speech offset runs ~0.84-1.62s, so a
    1.0s pad let short calls' windows close before voice audio even started
    (clipping mid-word). The default moved to 3.0 — and it has to be a
    setting, not a magic number, so it can be tuned per node without a code
    change (and so tests can prove it isn't hardcoded anywhere downstream).
    """
    assert settings.call_tail_pad_seconds == 3.0

    monkeypatch.setattr(settings, "call_tail_pad_seconds", 5.0)

    await tick(watcher, update(
        call_log=[grant(1234, clock.now)],
        channels=[channel(tgid=1234, srcaddr=555)],
    ))
    clock.advance(0.5)
    edge_time = clock.now
    await tick(watcher, update(channels=[channel(tgid=1234, srcaddr=0, hold_tgid=1234)]))

    # Advance well past both the idle timeout AND the monkeypatched 5.0s pad so
    # the "now" cap in _handle_channels never masks the pad value under test.
    clock.advance(settings.call_idle_timeout + 6.0)
    await tick(watcher, update(channels=[channel()]))

    payload = watcher.on_call_end.call_args[0][0]
    assert payload["ended_at_epoch"] == pytest.approx(edge_time + 5.0)


@pytest.mark.asyncio
async def test_short_call_window_now_covers_delayed_voice_arrival(watcher, clock):
    """
    Regression test for the truncation bug: a ~0.97s control-channel call
    (grant to srcaddr-drop) previously closed its window at
    last_tx_end + 1.0s pad, i.e. ~1.97s after the grant — but field
    measurement shows voice audio doesn't start until ~1.5s after the grant
    (0.84-1.62s measured), so the old window left as little as ~0.4s of
    captured speech and clipped it mid-word.

    With the 3.0s default pad, the same short call's window must extend well
    past the ~1.5s point where voice actually starts.
    """
    call_start = clock.now
    await tick(watcher, update(
        call_log=[grant(1234, call_start)],
        channels=[channel(tgid=1234, srcaddr=555)],
    ))

    # The control-channel call itself is short — under 1 second.
    clock.advance(0.97)
    edge_time = clock.now
    await tick(watcher, update(channels=[channel(tgid=1234, srcaddr=0, hold_tgid=1234)]))

    clock.advance(settings.call_idle_timeout + 0.5)
    await tick(watcher, update(channels=[channel()]))

    payload = watcher.on_call_end.call_args[0][0]
    assert payload["end_reason"] == "idle_timeout"

    voice_arrival = call_start + 1.5  # measured grant->speech offset, typical case
    assert payload["ended_at_epoch"] == pytest.approx(edge_time + settings.call_tail_pad_seconds)
    assert payload["ended_at_epoch"] > voice_arrival, (
        "recording window must extend past the point voice audio actually arrives, "
        "not just past the control-channel call_log timestamps"
    )


@pytest.mark.asyncio
async def test_idle_close_logs_the_measured_control_channel_idle(watcher, clock, caplog):
    """
    The correct idle timeout can only be tuned from the CONTROL-CHANNEL idle, not
    from silence measured in the audio (which also contains the ~1.9s P25
    grant→speech delay). So the real measured value has to reach the log.
    """
    await tick(watcher, update(
        call_log=[grant(1234, clock.now)],
        channels=[channel(tgid=1234, srcaddr=555)],
    ))
    clock.advance(0.5)
    await tick(watcher, update(channels=[channel(tgid=1234, srcaddr=0, hold_tgid=1234)]))

    with caplog.at_level("INFO", logger="drb-edge-node"):
        clock.advance(settings.call_idle_timeout + 0.25)
        await tick(watcher, update(channels=[channel()]))

    idle_lines = [r.message for r in caplog.records if "measured control-channel idle" in r.message]
    assert idle_lines, "idle-timeout closes must log the measured idle for later tuning"
    assert "3.25s" in idle_lines[0]


@pytest.mark.asyncio
async def test_ongoing_transmission_never_times_out(watcher, clock):
    await tick(watcher, update(
        call_log=[grant(1234, clock.now)],
        channels=[channel(tgid=1234, srcaddr=555)],
    ))

    for _ in range(20):
        clock.advance(settings.call_idle_timeout)
        await tick(watcher, update(channels=[channel(tgid=1234, srcaddr=555)]))
        assert watcher.is_active

    watcher.on_call_end.assert_not_called()


@pytest.mark.asyncio
async def test_op25_unreachable_ends_active_call_after_grace(watcher, clock):
    await tick(watcher, update(
        call_log=[grant(1234, clock.now)],
        channels=[channel(tgid=1234, srcaddr=555)],
    ))

    # A single failed poll must not kill the call.
    clock.advance(OP25_OFFLINE_GRACE / 2)
    await tick(watcher, None)
    assert watcher.is_active

    clock.advance(OP25_OFFLINE_GRACE)
    await tick(watcher, None)
    assert not watcher.is_active
    assert watcher.on_call_end.call_args[0][0]["end_reason"] == "op25_unreachable"


@pytest.mark.asyncio
async def test_grant_whose_call_already_ended(watcher, clock):
    """call_log delivers a start whose transmission is already over."""
    await tick(watcher, update(
        call_log=[grant(1234, clock.now - 1.2)],
        channels=[channel(tgid=1234, srcaddr=0, hold_tgid=1234)],  # already idle
    ))
    assert watcher.is_active

    clock.advance(settings.call_idle_timeout + 0.5)
    await tick(watcher, update(channels=[channel()]))

    assert not watcher.is_active
    watcher.on_call_end.assert_called_once()


@pytest.mark.asyncio
async def test_stop_closes_open_segment(watcher, clock):
    await tick(watcher, update(
        call_log=[grant(1234, clock.now)],
        channels=[channel(tgid=1234, srcaddr=555)],
    ))
    await watcher.stop()

    assert not watcher.is_active
    assert watcher.on_call_end.call_args[0][0]["end_reason"] == "shutdown"


# ---------------------------------------------------------------------------
# Segmentation: same-TGID continuation, different-TGID split
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_same_tgid_grant_continues_one_recording(watcher, clock):
    """Back-and-forth on one talkgroup must stay a single call/recording."""
    start = clock.now
    await tick(watcher, update(
        call_log=[grant(1234, start)],
        channels=[channel(tgid=1234, srcaddr=555)],
    ))
    call_id = watcher.active_call_id

    # Transmission ends...
    clock.advance(1.0)
    await tick(watcher, update(channels=[channel(tgid=1234, srcaddr=0, hold_tgid=1234)]))

    # ...and the other party keys up on the SAME tgid inside the idle window.
    clock.advance(1.0)
    await tick(watcher, update(
        call_log=[grant(1234, clock.now)],
        channels=[channel(tgid=1234, srcaddr=777)],
    ))

    assert watcher.is_active
    assert watcher.active_call_id == call_id, "same tgid must not open a new call"
    watcher.on_call_start.assert_called_once()
    watcher.on_call_end.assert_not_called()

    # And the whole exchange closes as one segment.
    clock.advance(1.0)
    await tick(watcher, update(channels=[channel(tgid=1234, srcaddr=0, hold_tgid=1234)]))
    clock.advance(settings.call_idle_timeout + 0.5)
    await tick(watcher, update(channels=[channel()]))

    watcher.on_call_end.assert_called_once()
    payload = watcher.on_call_end.call_args[0][0]
    assert payload["call_id"] == call_id
    assert payload["transmissions"] == 2
    assert payload["started_at_epoch"] == start


@pytest.mark.asyncio
async def test_different_tgid_grant_splits_recording(watcher, clock):
    await tick(watcher, update(
        call_log=[grant(1111, clock.now, tgtag="Fire")],
        channels=[channel(tgid=1111, srcaddr=1)],
    ))
    first_id = watcher.active_call_id

    clock.advance(2.0)
    split_time = clock.now
    await tick(watcher, update(
        call_log=[grant(2222, split_time, tgtag="EMS")],
        channels=[channel(tgid=2222, srcaddr=2)],
    ))

    assert watcher.is_active
    assert watcher.current_tgid == 2222
    assert watcher.active_call_id != first_id
    assert watcher.on_call_start.call_count == 2
    watcher.on_call_end.assert_called_once()

    ended = watcher.on_call_end.call_args[0][0]
    assert ended["call_id"] == first_id
    assert ended["tgid"] == 1111
    assert ended["end_reason"] == "tgid_change"
    # The outgoing segment is padded PAST where the new one begins. The buffered
    # audio lags control-channel timestamps by ~1.5s, so ending exactly at the
    # split cut the outgoing call's last words. The overlap is correct — the
    # audio stream really does hold one call's tail then the next call's start.
    assert ended["ended_at_epoch"] == split_time + settings.call_tail_pad_seconds
    assert watcher.on_call_start.call_args[0][0]["started_at_epoch"] == split_time


@pytest.mark.asyncio
async def test_multiple_call_log_entries_in_one_poll(watcher, clock):
    """
    The call_log deque is drained per poll and capped at 10, so one poll can
    deliver several grants. Same-tgid ones merge, different-tgid ones split.
    """
    t0 = clock.now - 1.5
    await tick(watcher, update(
        call_log=[
            grant(1111, t0),
            grant(1111, t0 + 0.4),   # same tgid -> continuation
            grant(2222, t0 + 0.9),   # different tgid -> split
        ],
        channels=[channel(tgid=2222, srcaddr=9)],
    ))

    assert watcher.current_tgid == 2222
    assert watcher.on_call_start.call_count == 2
    watcher.on_call_end.assert_called_once()

    ended = watcher.on_call_end.call_args[0][0]
    assert ended["tgid"] == 1111
    assert ended["transmissions"] == 2
    assert ended["started_at_epoch"] == t0
    # Split close is padded past the new grant — see the tgid_change test above.
    assert ended["ended_at_epoch"] == t0 + 0.9 + settings.call_tail_pad_seconds


@pytest.mark.asyncio
async def test_out_of_order_call_log_entries_are_sorted(watcher, clock):
    t0 = clock.now - 2.0
    await tick(watcher, update(
        call_log=[grant(2222, t0 + 1.0), grant(1111, t0)],
        channels=[channel(tgid=2222, srcaddr=9)],
    ))

    ended = watcher.on_call_end.call_args[0][0]
    assert ended["tgid"] == 1111
    assert watcher.current_tgid == 2222


@pytest.mark.asyncio
async def test_dropped_call_log_event_still_closes_segment(watcher, clock):
    """
    CALL_LOG_MAX_LEN is 10, so a slow consumer loses grants. If another talkgroup
    is plainly transmitting we must close immediately rather than record it under
    the wrong tgid for call_idle_timeout seconds.
    """
    await tick(watcher, update(
        call_log=[grant(1111, clock.now)],
        channels=[channel(tgid=1111, srcaddr=1)],
    ))
    first_id = watcher.active_call_id

    clock.advance(1.0)
    await tick(watcher, update(channels=[channel(tgid=3333, srcaddr=7)]))  # no call_log

    assert not watcher.is_active
    ended = watcher.on_call_end.call_args[0][0]
    assert ended["call_id"] == first_id
    assert ended["end_reason"] == "tgid_change_unlogged"


@pytest.mark.asyncio
async def test_other_receiver_on_another_tgid_does_not_split(watcher, clock):
    """
    The dropped-call_log fallback must not fire on multi-receiver setups: another
    receiver being busy says nothing about ours, and closing on it would truncate
    every call.
    """
    await tick(watcher, update(
        call_log=[grant(1111, clock.now)],
        channels=[channel(tgid=1111, srcaddr=1)],
    ))
    first_id = watcher.active_call_id

    clock.advance(0.5)
    await tick(watcher, update(channels=[
        channel(tgid=1111, srcaddr=0, hold_tgid=1111),
        channel(tgid=3333, srcaddr=7),
    ]))

    assert watcher.is_active
    assert watcher.active_call_id == first_id
    watcher.on_call_end.assert_not_called()


@pytest.mark.asyncio
async def test_second_receiver_activity_does_not_hold_segment_open(watcher, clock):
    """Only channels on OUR talkgroup count as our transmission."""
    await tick(watcher, update(
        call_log=[grant(1111, clock.now)],
        channels=[channel(tgid=1111, srcaddr=1)],
    ))

    clock.advance(0.5)
    await tick(watcher, update(channels=[
        channel(tgid=1111, srcaddr=0, hold_tgid=1111),
        channel(tgid=1111, srcaddr=0),
    ]))
    assert watcher.is_active

    clock.advance(settings.call_idle_timeout + 0.5)
    await tick(watcher, update(channels=[channel(tgid=1111, srcaddr=0, hold_tgid=1111)]))
    assert not watcher.is_active


@pytest.mark.asyncio
async def test_end_never_precedes_start(watcher, clock):
    """A clock skew or odd grant order must never produce a negative duration."""
    await tick(watcher, update(
        call_log=[grant(1234, clock.now + 5.0)],  # OP25 stamp in the future
        channels=[channel(tgid=1234, srcaddr=5)],
    ))
    await watcher.stop()

    payload = watcher.on_call_end.call_args[0][0]
    assert payload["ended_at_epoch"] >= payload["started_at_epoch"]


# ---------------------------------------------------------------------------
# AUDIO MODE — boundaries from the audio, label from the console
#
# This is the production path. Everything above this line is the fallback that
# only runs when PulseAudio capture is down.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_audio_onset_starts_the_recording(hearing, clock, audio):
    """Voice onset opens the segment, and the recorder is told to slice to it."""
    onset = audio.speak()
    await tick(hearing, update(
        call_log=[grant(1234, clock.now, tgtag="Police Dispatch")],
        channels=[channel(tgid=1234, srcaddr=555)],
    ))

    assert hearing.is_active
    hearing.on_call_start.assert_called_once()
    payload = hearing.on_call_start.call_args[0][0]
    assert payload["driver"] == "audio"
    # The recorder slices the ring buffer back to THIS epoch, so it has to be
    # the audio onset, not the grant and not our detection time.
    assert payload["started_at_epoch"] == onset
    assert payload["tgid"] == 1234
    assert payload["tgid_name"] == "Police Dispatch"
    assert payload["attributed"] is True


@pytest.mark.asyncio
async def test_a_grant_with_no_audio_does_not_start_a_recording(hearing, clock):
    """
    The grant fires 0.84-1.62s before anyone speaks. Opening on it is what put
    seconds of dead air at the head of every recording.
    """
    await tick(hearing, update(
        call_log=[grant(1234, clock.now, tgtag="Fire")],
        channels=[channel(tgid=1234, srcaddr=555)],
    ))

    assert not hearing.is_active
    hearing.on_call_start.assert_not_called()


@pytest.mark.asyncio
async def test_recording_closes_after_the_configured_silence(hearing, clock, audio):
    audio.speak()
    await tick(hearing, update(
        call_log=[grant(1234, clock.now)],
        channels=[channel(tgid=1234, srcaddr=555)],
    ))

    clock.advance(0.5)
    last_voice = audio.speak()
    await tick(hearing, update(channels=[channel(tgid=1234, srcaddr=555)]))

    # Quiet, but not for long enough yet.
    clock.advance(settings.call_silence_timeout - 0.5)
    await tick(hearing, update(channels=[channel()]))
    assert hearing.is_active
    hearing.on_call_end.assert_not_called()

    clock.advance(0.6)
    await tick(hearing, update(channels=[channel()]))

    assert not hearing.is_active
    payload = hearing.on_call_end.call_args[0][0]
    assert payload["end_reason"] == "audio_silence"
    assert payload["tgid"] == 1234
    # The audio ends where the silence run began plus the threshold, so the
    # trim can measure and strip exactly that run.
    assert payload["ended_at_epoch"] == pytest.approx(last_voice + settings.call_silence_timeout)


@pytest.mark.asyncio
async def test_a_false_srcaddr_drop_mid_speech_does_not_end_the_recording(hearing, clock, audio):
    """
    THE BUG THIS REARCHITECTURE EXISTS FOR. srcaddr can reset to 0 while someone
    is still talking; under the old design that started the idle timer and the
    window closed on top of live speech (recording 0ff35b20: "-1.61s lead,
    -0.00s tail" — nothing left to trim because the cut landed mid-word).
    """
    audio.speak()
    await tick(hearing, update(
        call_log=[grant(1234, clock.now)],
        channels=[channel(tgid=1234, srcaddr=555)],
    ))
    call_id = hearing.active_call_id

    # The control channel now says the call is over. It is wrong; the audio
    # keeps arriving. This runs well past call_idle_timeout, which is what
    # would have closed the segment before.
    for _ in range(10):
        clock.advance(0.5)
        audio.speak()
        await tick(hearing, update(channels=[channel(tgid=1234, srcaddr=0, hold_tgid=1234)]))
        assert hearing.is_active, "a false srcaddr drop must never end a recording"

    assert (clock.now - hearing._started_at) > settings.call_idle_timeout
    assert hearing.active_call_id == call_id
    hearing.on_call_end.assert_not_called()


@pytest.mark.asyncio
async def test_silence_close_logs_the_measured_trailing_silence(hearing, clock, audio, caplog):
    """
    call_silence_timeout can only be tuned from the real trailing silence, so
    the measured number has to reach the log — the audio-mode counterpart of
    the "measured control-channel idle" line.
    """
    audio.speak()
    await tick(hearing, update(call_log=[grant(1234, clock.now)]))

    with caplog.at_level("INFO", logger="drb-edge-node"):
        clock.advance(settings.call_silence_timeout + 0.25)
        await tick(hearing, update(channels=[channel()]))

    lines = [r.message for r in caplog.records if "measured trailing silence" in r.message]
    assert lines, "audio closes must log the measured silence for later tuning"
    assert "3.25s" in lines[0]


@pytest.mark.asyncio
async def test_same_tgid_grant_continues_one_audio_recording(hearing, clock, audio):
    """Back-and-forth on one talkgroup must stay a single call/recording."""
    onset = audio.speak()
    await tick(hearing, update(
        call_log=[grant(1234, clock.now)],
        channels=[channel(tgid=1234, srcaddr=555)],
    ))
    call_id = hearing.active_call_id

    # The other party keys up on the SAME tgid while audio is still flowing.
    clock.advance(1.0)
    audio.speak()
    await tick(hearing, update(
        call_log=[grant(1234, clock.now)],
        channels=[channel(tgid=1234, srcaddr=777)],
    ))

    assert hearing.active_call_id == call_id, "same tgid must not open a new call"
    hearing.on_call_start.assert_called_once()
    hearing.on_call_end.assert_not_called()

    clock.advance(settings.call_silence_timeout + 0.5)
    await tick(hearing, update(channels=[channel()]))

    hearing.on_call_end.assert_called_once()
    payload = hearing.on_call_end.call_args[0][0]
    assert payload["call_id"] == call_id
    assert payload["started_at_epoch"] == onset
    assert payload["transmissions"] == 2


@pytest.mark.asyncio
async def test_different_tgid_splits_even_with_no_silence_between(hearing, clock, audio):
    """
    Back-to-back calls on two talkgroups with no gap. Pure audio segmentation
    would merge them into ONE file under ONE label, which corrupts correlation.
    The console talkgroup change has to force the cut.
    """
    audio.speak()
    await tick(hearing, update(
        call_log=[grant(1111, clock.now, tgtag="Fire")],
        channels=[channel(tgid=1111, srcaddr=1)],
    ))
    first_id = hearing.active_call_id

    clock.advance(2.0)
    audio.speak()  # still talking — no silence anywhere in this test
    split = clock.now
    await tick(hearing, update(
        call_log=[grant(2222, split, tgtag="EMS")],
        channels=[channel(tgid=2222, srcaddr=2)],
    ))

    hearing.on_call_end.assert_called_once()
    ended = hearing.on_call_end.call_args[0][0]
    assert ended["call_id"] == first_id
    assert ended["tgid"] == 1111
    assert ended["end_reason"] == "tgid_change"
    # Padded past the split: buffered audio lags the control channel, so cutting
    # at the exact grant timestamp clipped the outgoing call's last words. The
    # overlap between the two slices is correct.
    assert ended["ended_at_epoch"] == split + settings.call_tail_pad_seconds

    assert hearing.is_active and hearing.current_tgid == 2222
    assert hearing.active_call_id != first_id
    assert hearing.on_call_start.call_count == 2
    assert hearing.on_call_start.call_args[0][0]["started_at_epoch"] == split


@pytest.mark.asyncio
async def test_an_unlogged_tgid_change_also_splits_and_reopens(hearing, clock, audio):
    """
    OP25's call_log deque is capped at 10, so grants get dropped. If our only
    receiver is plainly on another talkgroup the segment is over — and in audio
    mode a new one must open immediately or the audio would be dropped on the
    floor until the next voice run.
    """
    audio.speak()
    await tick(hearing, update(
        call_log=[grant(1111, clock.now)],
        channels=[channel(tgid=1111, srcaddr=1)],
    ))
    first_id = hearing.active_call_id

    clock.advance(1.0)
    audio.speak()
    await tick(hearing, update(channels=[channel(tgid=3333, srcaddr=7)]))  # no call_log

    ended = hearing.on_call_end.call_args[0][0]
    assert ended["call_id"] == first_id
    assert ended["end_reason"] == "tgid_change_unlogged"
    assert hearing.is_active and hearing.current_tgid == 3333


@pytest.mark.asyncio
async def test_max_length_backstop_closes_and_reopens(hearing, clock, audio):
    """
    A talkgroup that never goes quiet must not produce an unbounded recording —
    but the audio must not be dropped either, so the segment is immediately
    reopened rather than simply abandoned.
    """
    audio.speak()
    await tick(hearing, update(call_log=[grant(1234, clock.now)]))
    first_id = hearing.active_call_id

    clock.advance(MAX_SEGMENT_SECONDS / 2)
    audio.speak()
    await tick(hearing, update(channels=[channel(tgid=1234, srcaddr=5)]))
    assert hearing.active_call_id == first_id

    clock.advance(MAX_SEGMENT_SECONDS / 2 + 1)
    audio.speak()
    await tick(hearing, update(channels=[channel(tgid=1234, srcaddr=5)]))

    ended = hearing.on_call_end.call_args[0][0]
    assert ended["call_id"] == first_id
    assert ended["end_reason"] == "max_length"
    assert hearing.is_active, "a still-live transmission must not be dropped at the cap"
    assert hearing.active_call_id != first_id
    assert hearing.on_call_start.call_count == 2


# ---------------------------------------------------------------------------
# Attribution: resolved at close, from a bounded rolling console history
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_orphan_audio_is_discarded_flagged_and_counted(hearing, clock, audio, caplog):
    """
    Audio with no console talkgroup anywhere near it — Liquidsoap fallback, a
    test tone, stray noise, a dropped call_log. It must be impossible to miss
    and must never be uploaded: an untagged call poisons correlation.
    """
    audio.speak()
    await tick(hearing, update())  # console says nothing at all

    assert hearing.is_active
    started = hearing.on_call_start.call_args[0][0]
    assert started["tgid"] is None
    assert started["attributed"] is False

    with caplog.at_level("ERROR", logger="drb-edge-node"):
        clock.advance(settings.call_silence_timeout + 0.5)
        await tick(hearing, update())

    ended = hearing.on_call_end.call_args[0][0]
    assert ended["attributed"] is False
    assert ended["tgid"] is None
    assert hearing.unattributed_segments == 1
    assert any("ORPHAN AUDIO" in r.message for r in caplog.records), \
        "unattributed audio must be loud in the logs, not silent"


@pytest.mark.asyncio
async def test_a_grant_before_audio_onset_still_attributes_the_recording(hearing, clock, audio):
    """
    The common ordering: the console grants the channel, then 0.84-1.62s later
    (plus pipeline lag) the audio shows up. The lookback has to cover it.
    """
    grant_time = clock.now
    await tick(hearing, update(call_log=[grant(1234, grant_time, tgtag="Fire")]))
    assert not hearing.is_active

    clock.advance(2.0)
    audio.speak()
    await tick(hearing, update())  # console says nothing NOW

    assert hearing.is_active
    payload = hearing.on_call_start.call_args[0][0]
    assert payload["tgid"] == 1234
    assert payload["tgid_name"] == "Fire"


@pytest.mark.asyncio
async def test_a_grant_after_audio_onset_attributes_the_recording_late(hearing, clock, audio):
    """
    There is no guaranteed ordering: the console is polled every 500ms, so the
    grant can land after voice onset. The segment starts unattributed and picks
    the talkgroup up part-way through — expected, and fine.
    """
    audio.speak()
    await tick(hearing, update())

    assert hearing.is_active
    assert hearing.on_call_start.call_args[0][0]["attributed"] is False

    clock.advance(0.5)
    audio.speak()
    await tick(hearing, update(call_log=[grant(1234, clock.now, tgtag="EMS")]))
    assert hearing.current_tgid == 1234

    clock.advance(settings.call_silence_timeout + 0.5)
    await tick(hearing, update())

    ended = hearing.on_call_end.call_args[0][0]
    assert ended["attributed"] is True
    assert ended["tgid"] == 1234
    assert ended["tgid_name"] == "EMS"
    assert hearing.unattributed_segments == 0


@pytest.mark.asyncio
async def test_attribution_resolves_from_a_channel_row_when_the_grant_was_dropped(hearing, clock, audio):
    """A dropped grant is survivable: an active channel row names the talkgroup."""
    audio.speak()
    await tick(hearing, update())
    assert hearing.on_call_start.call_args[0][0]["tgid"] is None

    clock.advance(0.5)
    audio.speak()
    await tick(hearing, update(channels=[channel(tgid=4321, srcaddr=99, tag="Sheriff")]))

    clock.advance(settings.call_silence_timeout + 0.5)
    await tick(hearing, update())

    ended = hearing.on_call_end.call_args[0][0]
    assert ended["tgid"] == 4321
    assert ended["attributed"] is True


@pytest.mark.asyncio
async def test_console_activity_outside_the_tolerance_does_not_attribute(hearing, clock, audio):
    """
    The tolerance is deliberately bounded. A grant from long before the audio is
    not evidence about this audio, and borrowing it would be worse than
    admitting the audio is unattributed.
    """
    await tick(hearing, update(call_log=[grant(1234, clock.now)]))

    clock.advance(ATTRIBUTION_LOOKBACK_SECONDS + 5.0)
    audio.speak()
    await tick(hearing, update())

    assert hearing.is_active
    assert hearing.on_call_start.call_args[0][0]["tgid"] is None
    assert ATTRIBUTION_LOOKAHEAD_SECONDS > 0


@pytest.mark.asyncio
async def test_a_directly_stated_talkgroup_is_not_overruled_at_close(hearing, clock, audio, caplog):
    """
    The attribution window extends past the audio on purpose, so a neighbouring
    call's console activity can fall inside it. An inference over that window
    must never overrule a talkgroup the console stated outright — but the
    overlap does mean the split logic missed something, so it is logged.
    """
    audio.speak()
    await tick(hearing, update(
        call_log=[grant(1111, clock.now, tgtag="Fire")],
        channels=[channel(tgid=1111, srcaddr=1)],
    ))

    # A second talkgroup is busy on ANOTHER receiver, so no split fires, and it
    # produces more console observations than ours did.
    for _ in range(4):
        clock.advance(0.5)
        audio.speak()
        await tick(hearing, update(channels=[
            channel(tgid=1111, srcaddr=0, hold_tgid=1111),
            channel(tgid=2222, srcaddr=7),
        ]))

    with caplog.at_level("WARNING", logger="drb-edge-node"):
        clock.advance(settings.call_silence_timeout + 0.5)
        await tick(hearing, update())

    ended = hearing.on_call_end.call_args[0][0]
    assert ended["tgid"] == 1111, "the console said 1111 directly; inference must not overrule it"
    assert any("split logic should have fired" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_the_same_voice_run_does_not_reopen_a_second_recording(hearing, clock, audio):
    """A closed run must stay closed; only NEW audio opens the next segment."""
    audio.speak()
    await tick(hearing, update(call_log=[grant(1234, clock.now)]))

    clock.advance(settings.call_silence_timeout + 0.5)
    await tick(hearing, update())
    assert not hearing.is_active

    # Several more quiet polls must not resurrect it.
    for _ in range(3):
        clock.advance(0.5)
        await tick(hearing, update())
    assert not hearing.is_active
    assert hearing.on_call_start.call_count == 1

    # ...but the next voice run does open a new segment.
    clock.advance(1.0)
    audio.speak()
    await tick(hearing, update(call_log=[grant(1234, clock.now)]))
    assert hearing.is_active
    assert hearing.on_call_start.call_count == 2


# ---------------------------------------------------------------------------
# Mode changes: capture loss, console loss
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_losing_capture_closes_an_audio_segment(hearing, clock, audio):
    """
    An audio-driven segment must never hang open when the audio stops arriving:
    with no chunks there is no silence to detect, so the mode change is the
    thing that has to close it.
    """
    audio.speak()
    await tick(hearing, update(call_log=[grant(1234, clock.now)]))
    assert hearing.is_active

    audio.capturing = False
    clock.advance(0.5)
    await tick(hearing, update(channels=[channel(tgid=1234, srcaddr=555)]))

    assert not hearing.is_active
    assert hearing.on_call_end.call_args[0][0]["end_reason"] == "capture_lost"


@pytest.mark.asyncio
async def test_console_segmentation_takes_over_while_capture_is_down(hearing, clock, audio):
    """
    No audio means no recordings, but the node must still report real radio
    activity to C2 rather than going silent about it.
    """
    audio.capturing = False

    await tick(hearing, update(
        call_log=[grant(1234, clock.now, tgtag="Police")],
        channels=[channel(tgid=1234, srcaddr=555)],
    ))
    assert hearing.is_active
    assert hearing.on_call_start.call_args[0][0]["driver"] == "console"

    clock.advance(0.5)
    await tick(hearing, update(channels=[channel(tgid=1234, srcaddr=0, hold_tgid=1234)]))
    clock.advance(settings.call_idle_timeout + 0.5)
    await tick(hearing, update(channels=[channel()]))

    assert not hearing.is_active
    assert hearing.on_call_end.call_args[0][0]["end_reason"] == "idle_timeout"


@pytest.mark.asyncio
async def test_op25_unreachable_still_closes_an_audio_segment(hearing, clock, audio):
    """Without the console there is no attribution, so there is nothing to keep open."""
    audio.speak()
    await tick(hearing, update(call_log=[grant(1234, clock.now)]))

    clock.advance(OP25_OFFLINE_GRACE + 0.5)
    audio.speak()
    await tick(hearing, None)

    assert not hearing.is_active
    assert hearing.on_call_end.call_args[0][0]["end_reason"] == "op25_unreachable"


@pytest.mark.asyncio
async def test_console_history_is_bounded(hearing, clock, audio):
    """A rolling history that grows without limit would be a slow memory leak."""
    from app.internal.metadata_watcher import CONSOLE_HISTORY_MAX, CONSOLE_HISTORY_SECONDS

    for _ in range(CONSOLE_HISTORY_MAX + 200):
        clock.advance(0.05)
        await tick(hearing, update(channels=[channel(tgid=1234, srcaddr=5)]))

    assert len(hearing._console) <= CONSOLE_HISTORY_MAX

    # ...and old entries age out even when the count is low.
    clock.advance(CONSOLE_HISTORY_SECONDS + 1)
    await tick(hearing, update())
    assert all(e.epoch >= clock.now - CONSOLE_HISTORY_SECONDS for e in hearing._console)


# ---------------------------------------------------------------------------
# Every radio on the call (server-26#304) — not just the last srcaddr
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_call_end_lists_every_radio_in_order(watcher, clock):
    """Dispatcher, unit, dispatcher on one talkgroup: three transmissions."""
    start = clock.now
    await tick(watcher, update(call_log=[grant(1234, start, rid=555)],
                               channels=[channel(tgid=1234, srcaddr=555)]))
    clock.advance(1.0)
    await tick(watcher, update(channels=[channel(tgid=1234, srcaddr=555)]))
    clock.advance(1.0)
    await tick(watcher, update(channels=[channel(tgid=1234, srcaddr=777)]))
    clock.advance(0.5)
    await tick(watcher, update(channels=[channel(tgid=1234, srcaddr=777)]))
    clock.advance(1.0)
    await tick(watcher, update(channels=[channel(tgid=1234, srcaddr=555)]))
    clock.advance(0.5)
    await tick(watcher, update(channels=[channel(tgid=1234, srcaddr=0, hold_tgid=1234)]))
    clock.advance(settings.call_idle_timeout + 0.5)
    await tick(watcher, update(channels=[channel()]))

    payload = watcher.on_call_end.call_args[0][0]
    assert [r["radio"] for r in payload["radios"]] == [555, 777, 555]
    assert payload["radios"][0] == {"radio": 555, "start": 0.0, "end": 1.0}
    assert payload["radios"][1] == {"radio": 777, "start": 2.0, "end": 2.5}
    # srcaddr keeps its old meaning (the latest talker) for existing readers.
    assert payload["srcaddr"] == 555


@pytest.mark.asyncio
async def test_missed_polls_inside_one_keyup_stay_one_transmission(watcher, clock):
    """The radio ID drops out of some polls mid-transmission; that is not a new key-up."""
    await tick(watcher, update(call_log=[grant(1234, clock.now, rid=555)],
                               channels=[channel(tgid=1234, srcaddr=555)]))
    clock.advance(0.5)
    await tick(watcher, update(channels=[channel(tgid=1234, srcaddr=0, hold_tgid=1234)]))
    clock.advance(1.0)
    await tick(watcher, update(channels=[channel(tgid=1234, srcaddr=555)]))
    clock.advance(0.5)
    await tick(watcher, update(channels=[channel(tgid=1234, srcaddr=0, hold_tgid=1234)]))
    clock.advance(settings.call_idle_timeout + 0.5)
    await tick(watcher, update(channels=[channel()]))

    radios = watcher.on_call_end.call_args[0][0]["radios"]
    assert radios == [{"radio": 555, "start": 0.0, "end": 1.5}]


@pytest.mark.asyncio
async def test_radio_list_resets_between_calls(watcher, clock):
    await tick(watcher, update(call_log=[grant(1234, clock.now, rid=555)]))
    clock.advance(0.2)
    await tick(watcher, update(call_log=[grant(4321, clock.now, rid=888)]))

    first = watcher.on_call_end.call_args[0][0]
    assert [r["radio"] for r in first["radios"]] == [555]
    assert watcher._tx == [[888, clock.now, clock.now]]


@pytest.mark.asyncio
async def test_audio_mode_records_a_radio_seen_only_in_a_grant(hearing, clock, audio):
    """A short key-up between channel polls is only visible as a call_log grant."""
    audio.speak()
    await tick(hearing, update(call_log=[grant(1234, clock.now, rid=555)],
                               channels=[channel(tgid=1234, srcaddr=555)]))
    clock.advance(1.0)
    audio.speak()
    await tick(hearing, update(call_log=[grant(1234, clock.now, rid=777)]))
    clock.advance(settings.call_silence_timeout + 0.5)
    await tick(hearing, update(channels=[channel()]))

    radios = hearing.on_call_end.call_args[0][0]["radios"]
    assert [r["radio"] for r in radios] == [555, 777]


def test_rebase_radios_moves_offsets_into_the_audio_frame():
    """The file starts 0.25 s of pre-roll before the call, then 1.0 s of lead was trimmed."""
    radios = [{"radio": 555, "start": 0.0, "end": 2.0}, {"radio": 777, "start": 3.0, "end": 9.0}]
    out = rebase_radios(radios, shift=-0.75, span=6.0)
    assert out == [{"radio": 555, "start": 0.0, "end": 1.25}, {"radio": 777, "start": 2.25, "end": 6.0}]
