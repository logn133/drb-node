"""
Unit tests for the CallRecorder pre-roll ring buffer and per-call accumulator.

No FFmpeg and no PulseAudio: chunks are pushed through _ingest() with a patched
clock, which is exactly what the capture loop does at runtime. Silence trimming
is disabled by default here and exercised separately with a stubbed trimmer.
"""
import asyncio
import itertools
import time
from typing import List
from unittest.mock import patch

import pytest

from app.config import settings
from app.internal import call_recorder as recorder_mod
from app.internal.audio_trim import TrimResult
from app.internal.call_recorder import (
    CallRecorder,
    MAX_RECORDING_BYTES,
    MAX_RECORDING_SECONDS,
    PRE_ROLL_SECONDS,
    RING_BUFFER_SECONDS,
)

T0 = 1_700_000_000.0
CHUNK_INTERVAL = 0.1  # seconds of audio per synthetic chunk


@pytest.fixture
def recorder(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "trim_silence", False)
    r = CallRecorder()
    r._recordings_dir = tmp_path
    r._capturing = True
    return r


def ingest(recorder, start: float, end: float, marker: bytes = b"A", index: int = 0) -> int:
    """Feed one chunk every CHUNK_INTERVAL seconds over [start, end) through _ingest."""
    stamps: List[float] = []
    chunks: List[bytes] = []
    ts = start
    while ts < end:
        stamps.append(ts)
        chunks.append(marker + str(index).encode() + b";")
        index += 1
        ts = round(ts + CHUNK_INTERVAL, 6)

    with patch("app.internal.call_recorder.time.time", side_effect=stamps):
        for chunk in chunks:
            recorder._ingest(chunk)
    return index


def fill(recorder, start: float, end: float, marker: bytes = b"A", index: int = 0) -> int:
    """Alias kept for readability where the accumulator is not the point."""
    return ingest(recorder, start, end, marker=marker, index=index)


def timestamps(recorder):
    return [ts for ts, _ in recorder._buffer]


def markers(path) -> List[str]:
    return path.read_bytes().decode().strip(";").split(";")


def indices(path) -> List[int]:
    return [int(m[1:]) for m in markers(path) if m[1:].isdigit()]


# ---------------------------------------------------------------------------
# Ring buffer trimming (pre-roll duty only)
# ---------------------------------------------------------------------------

def test_idle_buffer_keeps_only_the_rolling_window(recorder):
    for offset in range(0, int(RING_BUFFER_SECONDS) + 20):
        with patch("app.internal.call_recorder.time.time", return_value=T0 + offset):
            recorder._ingest(b"x" * 16)

    assert len(recorder._buffer) <= RING_BUFFER_SECONDS + 1
    assert min(timestamps(recorder)) >= (T0 + RING_BUFFER_SECONDS + 19) - RING_BUFFER_SECONDS


@pytest.mark.asyncio
async def test_ring_buffer_is_trimmed_even_while_recording(recorder):
    """
    The ring buffer serves PRE-ROLL only. An open recording must no longer pin
    it — that was the mechanism that made call length depend on buffer size.
    """
    index = ingest(recorder, T0, T0 + 2.0)
    await recorder.start_recording("call-1", start_epoch=T0 + 1.0)
    ingest(recorder, T0 + 2.0, T0 + 2.0 + RING_BUFFER_SECONDS + 10, index=index)

    assert recorder.buffered_seconds <= RING_BUFFER_SECONDS + 1
    # ...and the audio the ring buffer dropped is safe in the accumulator.
    assert recorder._active is not None
    assert recorder._active.chunks[0][0] == pytest.approx(T0 + 1.0 - PRE_ROLL_SECONDS, abs=CHUNK_INTERVAL)


# ---------------------------------------------------------------------------
# Call length must not be bounded by the ring buffer
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_call_longer_than_the_ring_buffer_is_captured_whole(recorder):
    call_length = RING_BUFFER_SECONDS * 2 + 5  # 65s against a 30s ring buffer
    grant = T0 + 1.0
    end = grant + call_length

    index = ingest(recorder, T0, grant)
    await recorder.start_recording("call-long", start_epoch=grant)
    ingest(recorder, grant, end + 1.0, index=index)

    rec = await recorder.stop_recording(end_epoch=end)
    assert rec is not None and rec.path is not None

    kept = indices(rec.path)
    # Contiguous: no hole anywhere in the middle of a 65s call.
    assert kept == list(range(kept[0], kept[-1] + 1))
    span = (kept[-1] - kept[0]) * CHUNK_INTERVAL
    assert span > RING_BUFFER_SECONDS, "call length must not be clamped by the ring buffer"
    assert span == pytest.approx(call_length + PRE_ROLL_SECONDS, abs=2 * CHUNK_INTERVAL)


@pytest.mark.asyncio
async def test_accumulator_stops_growing_at_the_memory_ceiling(recorder, caplog):
    """A runaway call must not be able to exhaust RAM on a Pi."""
    await recorder.start_recording("call-runaway", start_epoch=T0)

    big = b"z" * 64_000
    needed = (MAX_RECORDING_BYTES // len(big)) + 5
    ticks = itertools.count()
    with caplog.at_level("WARNING", logger="drb-edge-node"):
        with patch("app.internal.call_recorder.time.time",
                   side_effect=lambda: T0 + next(ticks) * 0.1):
            for _ in range(needed):
                recorder._ingest(big)

    assert recorder._active is not None
    assert recorder._active.total_bytes <= MAX_RECORDING_BYTES
    assert recorder._active.truncated_by_cap
    assert any("memory ceiling" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Pre-roll and slicing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_slice_starts_pre_roll_before_the_op25_timestamp(recorder):
    fill(recorder, T0, T0 + 10.0)
    grant_time = T0 + 5.0

    await recorder.start_recording("call-1", start_epoch=grant_time)
    assert recorder._active.slice_start == pytest.approx(grant_time - PRE_ROLL_SECONDS)

    rec = await recorder.stop_recording(end_epoch=grant_time + 2.0)
    assert rec is not None and rec.path is not None and rec.path.exists()

    first_ts = T0 + indices(rec.path)[0] * CHUNK_INTERVAL

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
    rec = await recorder.stop_recording(end_epoch=T0 + 3.05)
    last_ts = T0 + indices(rec.path)[-1] * CHUNK_INTERVAL

    assert last_ts >= T0 + 3.05, "the chunk covering the end instant must be kept"


@pytest.mark.asyncio
async def test_max_recording_seconds_caps_the_slice(recorder):
    index = fill(recorder, T0, T0 + 1.0)
    await recorder.start_recording("call-1", start_epoch=T0 + 1.0)
    ingest(recorder, T0 + 1.0, T0 + MAX_RECORDING_SECONDS + 60, index=index)

    rec = await recorder.stop_recording(end_epoch=T0 + MAX_RECORDING_SECONDS + 50)
    last_ts = T0 + indices(rec.path)[-1] * CHUNK_INTERVAL

    assert last_ts <= T0 + 1.0 + MAX_RECORDING_SECONDS + CHUNK_INTERVAL


# ---------------------------------------------------------------------------
# Tail wait — the fix for recordings that ended mid-word
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stop_waits_for_captured_audio_to_reach_the_call_end(recorder, caplog):
    """
    PulseAudio → FFmpeg → encoder → muxer → our pipe read has latency, so at the
    instant a call ends the newest captured chunk is OLDER than the end epoch.
    Slicing immediately cuts the last word off. stop_recording must wait for it.
    """
    index = ingest(recorder, T0, T0 + 4.0)
    await recorder.start_recording("call-1", start_epoch=T0 + 1.0)

    async def late_tail():
        await asyncio.sleep(0.15)
        with patch("app.internal.call_recorder.time.time", return_value=T0 + 4.6):
            recorder._ingest(b"TAIL;")

    task = asyncio.create_task(late_tail())
    with caplog.at_level("INFO", logger="drb-edge-node"):
        rec = await recorder.stop_recording(end_epoch=T0 + 4.5)
    await task

    assert rec is not None and rec.path is not None
    assert b"TAIL" in rec.path.read_bytes(), "the late-arriving tail must be in the file"
    assert any("Waited" in r.message and "tail" in r.message for r in caplog.records), \
        "a tail wait must be observable in the field logs"
    assert index  # sanity: the pre-roll fill actually ran


@pytest.mark.asyncio
async def test_tail_wait_is_bounded_and_warns_when_audio_never_arrives(recorder, caplog, monkeypatch):
    monkeypatch.setattr(recorder_mod, "TAIL_WAIT_TIMEOUT_SECONDS", 0.2)
    ingest(recorder, T0, T0 + 4.0)
    await recorder.start_recording("call-1", start_epoch=T0 + 1.0)

    started = time.monotonic()
    with caplog.at_level("WARNING", logger="drb-edge-node"):
        rec = await recorder.stop_recording(end_epoch=T0 + 10.0)
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, "the wait must be bounded, never open-ended"
    assert rec is not None and rec.path is not None, "a short tail still beats no recording"
    messages = [r.message for r in caplog.records]
    assert any("Tail wait" in m and "gave up" in m for m in messages)
    assert any("BUFFER CLAMP" in m for m in messages), "silent truncation must be loud"


@pytest.mark.asyncio
async def test_no_wait_when_the_buffer_already_covers_the_end(recorder, caplog):
    ingest(recorder, T0, T0 + 10.0)
    await recorder.start_recording("call-1", start_epoch=T0 + 1.0)

    started = time.monotonic()
    with caplog.at_level("INFO", logger="drb-edge-node"):
        await recorder.stop_recording(end_epoch=T0 + 3.0)
    assert (time.monotonic() - started) < 0.1
    assert not any("Waited" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Clamping must be loud
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pre_roll_earlier_than_buffer_start_is_clamped_and_warned(recorder, caplog):
    """A grant older than anything buffered must still produce a file, loudly."""
    ingest(recorder, T0 + 5.0, T0 + 10.0)  # buffer only covers T0+5 onwards

    with caplog.at_level("WARNING", logger="drb-edge-node"):
        await recorder.start_recording("call-1", start_epoch=T0)  # 5 s before the head
    rec = await recorder.stop_recording(end_epoch=T0 + 8.0)

    assert rec is not None and rec.path is not None and rec.path.stat().st_size > 0
    assert markers(rec.path)[0] == "A0", "slice should begin at the buffer head, not fail"
    # Buffer head is T0+5.0, requested slice start is T0-PRE_ROLL: everything in
    # between is audio we can never recover, and the number must be reported.
    assert rec.clamped_seconds == pytest.approx(5.0 + PRE_ROLL_SECONDS, abs=CHUNK_INTERVAL)
    assert any("BUFFER CLAMP" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_no_buffered_audio_returns_none(recorder):
    await recorder.start_recording("call-1", start_epoch=T0)
    assert await recorder.stop_recording(end_epoch=T0 + 2.0) is None


@pytest.mark.asyncio
async def test_start_epoch_omitted_falls_back_to_now(recorder):
    now = time.time()
    fill(recorder, now - 5.0, now)

    await recorder.start_recording("call-1")
    assert recorder._active.slice_start == pytest.approx(now - PRE_ROLL_SECONDS, abs=1.0)


# ---------------------------------------------------------------------------
# Silence trimming and timing metadata
# ---------------------------------------------------------------------------

async def _recorded(recorder, end_offset: float = 3.0):
    ingest(recorder, T0, T0 + 10.0)
    await recorder.start_recording("call-1", start_epoch=T0 + 1.0)
    return await recorder.stop_recording(end_epoch=T0 + end_offset)


@pytest.mark.asyncio
async def test_trimming_is_off_when_the_setting_is_off(recorder, monkeypatch):
    monkeypatch.setattr(settings, "trim_silence", False)
    called = False

    async def _never(*args, **kwargs):
        nonlocal called
        called = True
        return TrimResult(path=None)

    monkeypatch.setattr(recorder_mod.audio_trim, "trim_silence", _never)
    rec = await _recorded(recorder)
    assert rec is not None and not called


@pytest.mark.asyncio
async def test_trim_shifts_the_audio_bounds_but_not_the_call_bounds(recorder, monkeypatch):
    """
    Trimming changes audio duration, so the AUDIO's wall-clock bounds move.
    The call's own started_at/ended_at (owned by metadata_watcher) must not be
    redefined — the recorder only reports where the audio now sits.
    """
    monkeypatch.setattr(settings, "trim_silence", True)

    async def _trim(path, **kwargs):
        return TrimResult(path=path, lead=1.9, tail=0.4, duration_before=3.3,
                          duration_after=1.0, applied=True)

    monkeypatch.setattr(recorder_mod.audio_trim, "trim_silence", _trim)

    rec = await _recorded(recorder)
    assert rec is not None and rec.path is not None
    assert rec.lead_trimmed == pytest.approx(1.9)
    assert rec.tail_trimmed == pytest.approx(0.4)
    # Untrimmed slice was [T0+0.75, T0+3.0]; the audio now starts 1.9s later and
    # ends 0.4s earlier, which is exactly what downstream needs to map an audio
    # offset back to wall clock.
    assert rec.audio_start_epoch == pytest.approx(T0 + 1.0 - PRE_ROLL_SECONDS + 1.9, abs=CHUNK_INTERVAL)
    assert rec.audio_end_epoch == pytest.approx(T0 + 3.0 - 0.4, abs=CHUNK_INTERVAL)


@pytest.mark.asyncio
async def test_all_silence_recording_is_dropped_and_logged(recorder, monkeypatch, caplog):
    monkeypatch.setattr(settings, "trim_silence", True)
    seen = {}

    async def _trim(path, **kwargs):
        seen["path"] = path
        return TrimResult(path=path, duration_before=4.0, duration_after=4.0, all_silence=True)

    monkeypatch.setattr(recorder_mod.audio_trim, "trim_silence", _trim)

    with caplog.at_level("WARNING", logger="drb-edge-node"):
        rec = await _recorded(recorder)

    assert rec is not None
    assert rec.all_silence is True
    assert rec.path is None, "an all-silence recording must not be uploaded"
    assert not seen["path"].exists(), "the file must be cleaned up, not left on disk"
    assert any("no speech" in r.message for r in caplog.records)


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
    ingest(recorder, T0, T0 + 10.0)
    split = T0 + 5.0

    await recorder.start_recording("call-1", start_epoch=T0 + 1.0)
    first = await recorder.stop_recording(end_epoch=split)

    await recorder.start_recording("call-2", start_epoch=split)
    second = await recorder.stop_recording(end_epoch=T0 + 8.0)

    assert first is not None and first.path.stat().st_size > 0
    assert second is not None and second.path.stat().st_size > 0
    assert first.path != second.path


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


def test_memory_ceiling_covers_the_longest_allowed_call():
    """The cap must bound RAM without ever being able to truncate a legal call."""
    bytes_per_second = 16_000 // 8
    assert MAX_RECORDING_BYTES >= MAX_RECORDING_SECONDS * bytes_per_second


# ---------------------------------------------------------------------------
# Capture-exit classification — the two failure modes must be told apart
# instead of both logging the same generic "restarting" line. This is what
# let a wrong PULSE_SOURCE hide behind normal-looking startup retries before.
# ---------------------------------------------------------------------------

def _log_levels(caplog, logger_name="drb-edge-node"):
    return [r.levelname for r in caplog.records if r.name == logger_name]


def test_capture_exit_logs_error_when_source_missing(recorder, caplog):
    """FFmpeg's pulse input prints 'No such process' when the daemon is up
    but the configured source name does not exist — a real misconfiguration,
    not a startup race, so this must stand out as an error naming the source."""
    recorder._last_stderr_lines.append(
        "[pulse @ 0x...] pa_stream_connect_record failed: No such process"
    )
    with caplog.at_level("INFO", logger="drb-edge-node"):
        recorder._log_capture_exit()

    assert "ERROR" in _log_levels(caplog)
    error_messages = [r.message for r in caplog.records if r.levelname == "ERROR"]
    assert any(settings.pulse_source in m for m in error_messages)


def test_capture_exit_logs_info_when_no_daemon(recorder, caplog):
    """Connection refused means nothing is listening yet — expected during
    startup, so it must NOT be logged at the same severity as a real
    misconfiguration."""
    recorder._last_stderr_lines.append(
        "[pulse @ 0x...] pa_context_connect() failed: Connection refused"
    )
    with caplog.at_level("INFO", logger="drb-edge-node"):
        recorder._log_capture_exit()

    levels = _log_levels(caplog)
    assert "ERROR" not in levels
    assert "INFO" in levels


def test_capture_exit_falls_back_to_generic_warning(recorder, caplog):
    """An FFmpeg failure that matches neither known marker keeps the original
    generic behavior rather than guessing."""
    recorder._last_stderr_lines.append("[pulse @ 0x...] some other unexpected failure")
    with caplog.at_level("INFO", logger="drb-edge-node"):
        recorder._log_capture_exit()

    assert _log_levels(caplog) == ["WARNING"]


def test_capture_exit_with_no_stderr_captured_is_generic_warning(recorder, caplog):
    """No stderr at all (e.g. FFmpeg killed before printing anything) must not
    crash the classifier and must fall back to the generic message."""
    assert list(recorder._last_stderr_lines) == []
    with caplog.at_level("INFO", logger="drb-edge-node"):
        recorder._log_capture_exit()

    assert _log_levels(caplog) == ["WARNING"]
    assert MAX_RECORDING_BYTES <= 8 * 1024 * 1024, "must stay small enough for a Pi"
