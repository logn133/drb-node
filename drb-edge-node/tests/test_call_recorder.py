"""
Unit tests for the CallRecorder ring buffer and per-call slicing.

No FFmpeg and no PulseAudio: the buffer is filled directly with timestamped
chunks, which is exactly what _ingest() produces at runtime.
"""
import time
from unittest.mock import patch

import pytest

from app.internal.call_recorder import (
    CallRecorder,
    MAX_RECORDING_SECONDS,
    PRE_ROLL_SECONDS,
    RING_BUFFER_SECONDS,
)

T0 = 1_700_000_000.0
CHUNK_INTERVAL = 0.1  # seconds of audio per synthetic chunk


@pytest.fixture
def recorder(tmp_path):
    r = CallRecorder()
    r._recordings_dir = tmp_path
    r._capturing = True
    return r


def fill(recorder, start: float, end: float, marker: bytes = b"A"):
    """Append one chunk every CHUNK_INTERVAL seconds over [start, end)."""
    ts = start
    index = 0
    while ts < end:
        recorder._buffer.append((ts, marker + str(index).encode() + b";"))
        index += 1
        ts = round(ts + CHUNK_INTERVAL, 6)


def timestamps(recorder):
    return [ts for ts, _ in recorder._buffer]


# ---------------------------------------------------------------------------
# Ring buffer trimming
# ---------------------------------------------------------------------------

def test_idle_buffer_keeps_only_the_rolling_window(recorder):
    with patch("app.internal.call_recorder.time.time", return_value=T0):
        for offset in range(0, int(RING_BUFFER_SECONDS) + 20):
            with patch("app.internal.call_recorder.time.time", return_value=T0 + offset):
                recorder._ingest(b"x" * 16)

    assert len(recorder._buffer) <= RING_BUFFER_SECONDS + 1
    assert min(timestamps(recorder)) >= (T0 + RING_BUFFER_SECONDS + 19) - RING_BUFFER_SECONDS


@pytest.mark.asyncio
async def test_buffer_is_not_trimmed_below_the_active_slice(recorder):
    fill(recorder, T0, T0 + 5.0)
    await recorder.start_recording("call-1", start_epoch=T0 + 1.0)

    # Ingest far past the normal rolling window; the slice start must survive.
    with patch("app.internal.call_recorder.time.time", return_value=T0 + RING_BUFFER_SECONDS + 10):
        recorder._ingest(b"z")

    assert min(timestamps(recorder)) <= (T0 + 1.0) - PRE_ROLL_SECONDS + CHUNK_INTERVAL


# ---------------------------------------------------------------------------
# Pre-roll and slicing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_slice_starts_pre_roll_before_the_op25_timestamp(recorder):
    fill(recorder, T0, T0 + 10.0)
    grant_time = T0 + 5.0

    await recorder.start_recording("call-1", start_epoch=grant_time)
    assert recorder._slice_start == pytest.approx(grant_time - PRE_ROLL_SECONDS)

    path = await recorder.stop_recording(end_epoch=grant_time + 2.0)
    assert path is not None and path.exists()

    # Reconstruct which chunks landed in the file.
    kept = path.read_bytes().decode().strip(";").split(";")
    first_index = int(kept[0][1:])
    first_ts = T0 + first_index * CHUNK_INTERVAL

    # A chunk stamped `ts` holds the audio that arrived over [ts - interval, ts],
    # so the audio actually covered must begin at or before the requested slice
    # start — erring early is the safe direction, erring late loses speech.
    assert first_ts - CHUNK_INTERVAL <= grant_time - PRE_ROLL_SECONDS + 1e-6
    # ...and no more than one chunk of extra pre-roll is dragged in.
    assert first_ts >= grant_time - PRE_ROLL_SECONDS - 1e-6


@pytest.mark.asyncio
async def test_tail_chunk_straddling_the_end_is_included(recorder):
    fill(recorder, T0, T0 + 10.0)
    await recorder.start_recording("call-1", start_epoch=T0 + 1.0)

    # End halfway through a chunk interval.
    path = await recorder.stop_recording(end_epoch=T0 + 3.05)
    kept = path.read_bytes().decode().strip(";").split(";")
    last_ts = T0 + int(kept[-1][1:]) * CHUNK_INTERVAL

    assert last_ts >= T0 + 3.05, "the chunk covering the end instant must be kept"


@pytest.mark.asyncio
async def test_pre_roll_earlier_than_buffer_start_is_clamped(recorder, caplog):
    """A grant older than anything buffered must still produce a file."""
    fill(recorder, T0 + 5.0, T0 + 10.0)  # buffer only covers T0+5 onwards

    await recorder.start_recording("call-1", start_epoch=T0)  # 5 s before the head
    path = await recorder.stop_recording(end_epoch=T0 + 8.0)

    assert path is not None and path.stat().st_size > 0
    kept = path.read_bytes().decode().strip(";").split(";")
    assert kept[0] == "A0", "slice should begin at the buffer head, not fail"


@pytest.mark.asyncio
async def test_no_buffered_audio_returns_none(recorder):
    await recorder.start_recording("call-1", start_epoch=T0)
    assert await recorder.stop_recording(end_epoch=T0 + 2.0) is None


@pytest.mark.asyncio
async def test_start_epoch_omitted_falls_back_to_now(recorder):
    now = time.time()
    fill(recorder, now - 5.0, now)

    await recorder.start_recording("call-1")
    assert recorder._slice_start == pytest.approx(now - PRE_ROLL_SECONDS, abs=1.0)


@pytest.mark.asyncio
async def test_max_recording_seconds_caps_the_slice(recorder):
    fill(recorder, T0, T0 + MAX_RECORDING_SECONDS + 60, marker=b"A")
    await recorder.start_recording("call-1", start_epoch=T0 + 1.0)

    path = await recorder.stop_recording(end_epoch=T0 + MAX_RECORDING_SECONDS + 50)
    kept = path.read_bytes().decode().strip(";").split(";")
    last_ts = T0 + int(kept[-1][1:]) * CHUNK_INTERVAL

    assert last_ts <= T0 + 1.0 + MAX_RECORDING_SECONDS + CHUNK_INTERVAL


# ---------------------------------------------------------------------------
# Recording lifecycle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_second_start_is_rejected_while_recording(recorder):
    fill(recorder, T0, T0 + 5.0)
    assert await recorder.start_recording("call-1", start_epoch=T0 + 1.0) is True
    assert await recorder.start_recording("call-2", start_epoch=T0 + 2.0) is False
    assert recorder.is_recording


@pytest.mark.asyncio
async def test_stop_without_start_is_a_noop(recorder):
    assert await recorder.stop_recording() is None
    assert not recorder.is_recording


@pytest.mark.asyncio
async def test_split_then_immediate_restart_keeps_both_slices(recorder):
    """
    A tgid change closes one recording and opens the next at the same instant —
    the second must still find its pre-roll in the buffer.
    """
    fill(recorder, T0, T0 + 10.0)
    split = T0 + 5.0

    await recorder.start_recording("call-1", start_epoch=T0 + 1.0)
    first = await recorder.stop_recording(end_epoch=split)

    await recorder.start_recording("call-2", start_epoch=split)
    second = await recorder.stop_recording(end_epoch=T0 + 8.0)

    assert first is not None and first.stat().st_size > 0
    assert second is not None and second.stat().st_size > 0
    assert first != second


# ---------------------------------------------------------------------------
# FFmpeg invocation
# ---------------------------------------------------------------------------

def test_ffmpeg_command_reads_pulse_and_flushes_packets(recorder):
    cmd = recorder._ffmpeg_command()
    joined = " ".join(cmd)

    assert "-f pulse" in joined
    assert "drb_sink.monitor" in joined, "must address the monitor explicitly, not 'default'"
    # Without -flush_packets the mp3 muxer buffers 32 KB (~16 s at 16 kbps) before
    # writing, which would destroy the ring buffer's timestamp resolution.
    assert "-flush_packets" in cmd
    assert cmd[-1] == "-" and cmd[-2] == "mp3", "must emit MP3 on stdout for /upload"
