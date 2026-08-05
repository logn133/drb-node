"""
Unit tests for the event-driven MetadataWatcher state machine.

Call START comes from OP25 `call_log` entries (stamped with OP25's own
time.time()); call END comes from the srcaddr != 0 -> srcaddr == 0 transition in
`channel_update`.  All OP25 HTTP calls are mocked — no running services required.
"""
import pytest
from unittest.mock import AsyncMock, patch

from app.config import settings
from app.internal.metadata_watcher import (
    MetadataWatcher,
    OP25_OFFLINE_GRACE,
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
    w = MetadataWatcher()
    w._clock = clock
    w.on_call_start = AsyncMock()
    w.on_call_end = AsyncMock()
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
# Call end — srcaddr edge + idle timeout
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
async def test_tail_pad_is_configurable_and_defaults_to_one_second(watcher, clock, monkeypatch):
    """
    0.5s left only ~0.3s of real trailing margin in field measurement and one
    recording ended mid-word, so the default moved to 1.0 — and it has to be a
    setting, not a magic number, so it can be tuned per node.
    """
    assert settings.call_tail_pad_seconds == 1.0

    monkeypatch.setattr(settings, "call_tail_pad_seconds", 2.5)

    await tick(watcher, update(
        call_log=[grant(1234, clock.now)],
        channels=[channel(tgid=1234, srcaddr=555)],
    ))
    clock.advance(0.5)
    edge_time = clock.now
    await tick(watcher, update(channels=[channel(tgid=1234, srcaddr=0, hold_tgid=1234)]))

    clock.advance(settings.call_idle_timeout + 1.0)
    await tick(watcher, update(channels=[channel()]))

    payload = watcher.on_call_end.call_args[0][0]
    assert payload["ended_at_epoch"] == pytest.approx(edge_time + 2.5)


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
    # The outgoing segment ends exactly where the new one begins — no tail pad,
    # or it would swallow the first moments of the new talkgroup.
    assert ended["ended_at_epoch"] == split_time
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
    assert ended["ended_at_epoch"] == t0 + 0.9


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
