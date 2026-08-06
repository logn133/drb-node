"""
Unit tests for the CallRecorder: PCM ring buffer, per-call accumulator, the
continuous voice-activity signal the segmenter reads, and the single encode.

No FFmpeg and no PulseAudio: chunks are pushed through _ingest() with a patched
clock, which is exactly what the capture loop does at runtime, and the MP3
encoder is replaced with a stub that writes the raw PCM it was handed. That stub
is also how "exactly one encode per call" is asserted — the old design captured
MP3 and then re-encoded it to trim, so every upload was double-encoded.
"""
import asyncio
import itertools
import time
from array import array
from typing import List
from unittest.mock import patch

import pytest

from app.config import settings
from app.internal import call_recorder as recorder_mod
from app.internal import pcm
from app.internal.call_recorder import (
    CallRecorder,
    MAX_RECORDING_BYTES,
    MAX_RECORDING_SECONDS,
    PRE_ROLL_SECONDS,
    RING_BUFFER_SECONDS,
)

T0 = 1_700_000_000.0

# One synthetic chunk carries exactly CHUNK_INTERVAL seconds of audio AND
# arrives CHUNK_INTERVAL apart, so arrival-timestamp arithmetic (slicing) and
# byte-offset arithmetic (trimming) agree with each other.
CHUNK_INTERVAL = 0.1
CHUNK_SAMPLES = int(pcm.SAMPLE_RATE * CHUNK_INTERVAL)
CHUNK_BYTES = CHUNK_SAMPLES * pcm.FRAME_BYTES

SPEECH_LEVEL = 4096   # -18 dBFS, the measured field average
FLOOR_LEVEL = 1       # -90.3 dBFS, the measured digital-silence floor


def block(level: int, samples: int = CHUNK_SAMPLES) -> bytes:
    return array("h", [level, -level] * (samples // 2)).tobytes()


VOICE = block(SPEECH_LEVEL)
QUIET = block(FLOOR_LEVEL)


@pytest.fixture
def encodes(monkeypatch):
    """Replace the one encode with a stub that writes the PCM it was given."""
    calls: List[tuple] = []

    async def _encode(audio: bytes, path):
        calls.append((audio, path))
        path.write_bytes(audio)
        return True

    monkeypatch.setattr(recorder_mod, "encode_mp3", _encode)
    return calls


@pytest.fixture
def recorder(tmp_path, monkeypatch, encodes):
    monkeypatch.setattr(settings, "trim_silence", False)
    r = CallRecorder()
    r._recordings_dir = tmp_path
    r._capturing = True
    return r


def ingest(recorder, start: float, end: float, chunk: bytes = VOICE) -> None:
    """Feed one chunk every CHUNK_INTERVAL seconds over [start, end)."""
    stamps: List[float] = []
    ts = start
    while ts < end:
        stamps.append(ts)
        ts = round(ts + CHUNK_INTERVAL, 6)
    if not stamps:
        return
    with patch("app.internal.call_recorder.time.time", side_effect=stamps):
        for _ in stamps:
            recorder._ingest(chunk)


def duration_of(path) -> float:
    return pcm.seconds(len(path.read_bytes()))


def timestamps(recorder):
    return [ts for ts, _ in recorder._buffer]


# ---------------------------------------------------------------------------
# Ring buffer trimming (pre-roll duty only)
# ---------------------------------------------------------------------------

def test_idle_buffer_keeps_only_the_rolling_window(recorder):
    ingest(recorder, T0, T0 + RING_BUFFER_SECONDS + 20)

    assert recorder.buffered_seconds <= RING_BUFFER_SECONDS + CHUNK_INTERVAL
    newest = max(timestamps(recorder))
    assert min(timestamps(recorder)) >= newest - RING_BUFFER_SECONDS


@pytest.mark.asyncio
async def test_ring_buffer_is_trimmed_even_while_recording(recorder):
    """
    The ring buffer serves PRE-ROLL only. An open recording must not pin it —
    that was the mechanism that made call length depend on buffer size.
    """
    ingest(recorder, T0, T0 + 2.0)
    await recorder.start_recording("call-1", start_epoch=T0 + 1.0)
    ingest(recorder, T0 + 2.0, T0 + 2.0 + RING_BUFFER_SECONDS + 10)

    assert recorder.buffered_seconds <= RING_BUFFER_SECONDS + CHUNK_INTERVAL
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

    ingest(recorder, T0, grant)
    await recorder.start_recording("call-long", start_epoch=grant)
    ingest(recorder, grant, end + 1.0)

    rec = await recorder.stop_recording(end_epoch=end)
    assert rec is not None and rec.path is not None

    captured = duration_of(rec.path)
    assert captured > RING_BUFFER_SECONDS, "call length must not be clamped by the ring buffer"
    assert captured == pytest.approx(call_length + PRE_ROLL_SECONDS, abs=2 * CHUNK_INTERVAL)


@pytest.mark.asyncio
async def test_accumulator_stops_growing_at_the_memory_ceiling(recorder, caplog):
    """A runaway call must not be able to exhaust RAM on a Pi."""
    await recorder.start_recording("call-runaway", start_epoch=T0)

    # Silent blocks on purpose: this test is about bytes, not content, and the
    # all-zero fast path keeps it from spending seconds in the RMS loop.
    big = b"\x00" * 64_000
    needed = (MAX_RECORDING_BYTES // len(big)) + 5
    # An unbounded clock: patching time.time patches it for everything running
    # inside the block, not only for our calls.
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


def test_the_byte_ceiling_can_never_truncate_a_legal_call():
    """
    PCM costs 44.1 KB/s where MP3 cost 2 KB/s, so this had to be re-derived.
    The TIME cap must always bite before the BYTE cap, or a long pursuit would
    be silently cut short by a memory limit.
    """
    assert MAX_RECORDING_BYTES > MAX_RECORDING_SECONDS * pcm.BYTES_PER_SECOND
    # ...and it still has to be a deliberate, bounded number on a Pi.
    assert MAX_RECORDING_BYTES <= 48 * 1024 * 1024


def test_the_ring_buffer_memory_cost_is_bounded():
    assert RING_BUFFER_SECONDS * pcm.BYTES_PER_SECOND < 2 * 1024 * 1024


# ---------------------------------------------------------------------------
# Voice activity — the signal the segmenter starts and stops on
# ---------------------------------------------------------------------------

def test_digital_silence_produces_no_voice_marks(recorder):
    ingest(recorder, T0, T0 + 5.0, chunk=QUIET)

    activity = recorder.audio_activity()
    assert activity.last_voice_epoch is None
    assert activity.voice_onset_epoch is None


def test_voice_onset_is_the_arrival_of_the_first_non_silent_chunk(recorder):
    ingest(recorder, T0, T0 + 2.0, chunk=QUIET)
    ingest(recorder, T0 + 2.0, T0 + 3.0, chunk=VOICE)

    activity = recorder.audio_activity()
    assert activity.voice_onset_epoch == pytest.approx(T0 + 2.0)
    assert activity.last_voice_epoch == pytest.approx(T0 + 3.0 - CHUNK_INTERVAL)


def test_a_gap_shorter_than_the_silence_timeout_does_not_start_a_new_run(recorder, monkeypatch):
    """Back-and-forth inside the window is ONE run, hence one recording."""
    monkeypatch.setattr(settings, "call_silence_timeout", 3.0)
    ingest(recorder, T0, T0 + 1.0, chunk=VOICE)
    ingest(recorder, T0 + 1.0, T0 + 2.5, chunk=QUIET)
    ingest(recorder, T0 + 2.5, T0 + 3.5, chunk=VOICE)

    assert recorder.audio_activity().voice_onset_epoch == pytest.approx(T0)


def test_a_gap_longer_than_the_silence_timeout_starts_a_new_run(recorder, monkeypatch):
    monkeypatch.setattr(settings, "call_silence_timeout", 3.0)
    ingest(recorder, T0, T0 + 1.0, chunk=VOICE)
    ingest(recorder, T0 + 1.0, T0 + 6.0, chunk=QUIET)
    ingest(recorder, T0 + 6.0, T0 + 7.0, chunk=VOICE)

    assert recorder.audio_activity().voice_onset_epoch == pytest.approx(T0 + 6.0)


def test_activity_snapshot_reports_capture_and_recording_state(recorder):
    activity = recorder.audio_activity()
    assert activity.capturing is True
    assert activity.recording is False

    recorder._capturing = False
    assert recorder.audio_activity().capturing is False


# ---------------------------------------------------------------------------
# Pre-roll and slicing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_slice_starts_pre_roll_before_the_detected_onset(recorder):
    ingest(recorder, T0, T0 + 10.0)
    onset = T0 + 5.0

    await recorder.start_recording("call-1", start_epoch=onset)
    assert recorder._active.slice_start == pytest.approx(onset - PRE_ROLL_SECONDS)

    rec = await recorder.stop_recording(end_epoch=onset + 2.0)
    assert rec is not None and rec.path is not None and rec.path.exists()

    first_ts = recorder._active.chunks[0][0] if recorder._active else None
    assert first_ts is None  # recording closed
    # The audio actually covered must begin at or before the requested slice
    # start — erring early is safe, erring late loses speech.
    assert rec.audio_start_epoch <= onset - PRE_ROLL_SECONDS + 1e-6


@pytest.mark.asyncio
async def test_tail_chunk_straddling_the_end_is_included(recorder):
    ingest(recorder, T0, T0 + 10.0)
    await recorder.start_recording("call-1", start_epoch=T0 + 1.0)

    rec = await recorder.stop_recording(end_epoch=T0 + 3.05)

    # The chunk covering the end instant must be kept, so the captured audio
    # reaches past the requested end rather than stopping short of it.
    assert rec.audio_end_epoch >= T0 + 3.05


@pytest.mark.asyncio
async def test_max_recording_seconds_caps_the_slice(recorder):
    ingest(recorder, T0, T0 + 1.0)
    await recorder.start_recording("call-1", start_epoch=T0 + 1.0)
    ingest(recorder, T0 + 1.0, T0 + MAX_RECORDING_SECONDS + 60)

    rec = await recorder.stop_recording(end_epoch=T0 + MAX_RECORDING_SECONDS + 50)

    assert duration_of(rec.path) <= MAX_RECORDING_SECONDS + PRE_ROLL_SECONDS + CHUNK_INTERVAL


# ---------------------------------------------------------------------------
# Tail wait — still needed for control-channel-derived ends (tgid splits)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stop_waits_for_captured_audio_to_reach_the_call_end(recorder, caplog):
    """
    A tgid_change close pads past a control-channel timestamp that is ~now, so
    the audio it asks for has not been captured yet. Slicing immediately would
    cut the last word off.
    """
    ingest(recorder, T0, T0 + 4.0)
    await recorder.start_recording("call-1", start_epoch=T0 + 1.0)

    async def late_tail():
        await asyncio.sleep(0.15)
        with patch("app.internal.call_recorder.time.time", return_value=T0 + 4.6):
            recorder._ingest(block(SPEECH_LEVEL))

    task = asyncio.create_task(late_tail())
    with caplog.at_level("INFO", logger="drb-edge-node"):
        rec = await recorder.stop_recording(end_epoch=T0 + 4.5)
    await task

    assert rec is not None and rec.path is not None
    # The slice covers the chunks stamped T0+0.8 .. T0+3.9 (32 of them) plus the
    # one that arrived late — and that last one is where the final word of the
    # transmission lives. Without the wait it would have been cut.
    chunk_seconds = pcm.seconds(len(VOICE))
    assert duration_of(rec.path) == pytest.approx(33 * chunk_seconds, abs=0.01)
    assert duration_of(rec.path) > 32 * chunk_seconds
    assert any("Waited" in r.message and "tail" in r.message for r in caplog.records), \
        "a tail wait must be observable in the field logs"


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
    """
    An audio-driven close derives its end epoch from audio that is already
    buffered, so the common path must never pay the tail wait at all.
    """
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
    """An onset older than anything buffered must still produce a file, loudly."""
    ingest(recorder, T0 + 5.0, T0 + 10.0)  # buffer only covers T0+5 onwards

    with caplog.at_level("WARNING", logger="drb-edge-node"):
        await recorder.start_recording("call-1", start_epoch=T0)  # 5 s before the head
    rec = await recorder.stop_recording(end_epoch=T0 + 8.0)

    assert rec is not None and rec.path is not None and rec.path.stat().st_size > 0
    assert rec.clamped_seconds == pytest.approx(5.0 + PRE_ROLL_SECONDS, abs=CHUNK_INTERVAL)
    assert any("BUFFER CLAMP" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_no_buffered_audio_returns_none(recorder, encodes):
    await recorder.start_recording("call-1", start_epoch=T0)
    assert await recorder.stop_recording(end_epoch=T0 + 2.0) is None
    assert encodes == [], "nothing to encode means no encoder subprocess"


@pytest.mark.asyncio
async def test_start_epoch_omitted_falls_back_to_now(recorder):
    now = time.time()
    ingest(recorder, now - 5.0, now)

    await recorder.start_recording("call-1")
    assert recorder._active.slice_start == pytest.approx(now - PRE_ROLL_SECONDS, abs=1.0)


# ---------------------------------------------------------------------------
# Encode — exactly once, at save time
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_audio_is_encoded_exactly_once_per_call(recorder, encodes, monkeypatch):
    """
    The old pipeline captured MP3 and then re-encoded it to trim, so every
    upload was double-encoded. Capture is PCM now and MP3 happens once, after
    trimming, at save time.
    """
    monkeypatch.setattr(settings, "trim_silence", True)
    ingest(recorder, T0, T0 + 1.0, chunk=QUIET)
    ingest(recorder, T0 + 1.0, T0 + 3.0, chunk=VOICE)
    ingest(recorder, T0 + 3.0, T0 + 6.0, chunk=QUIET)

    await recorder.start_recording("call-1", start_epoch=T0 + 1.0)
    rec = await recorder.stop_recording(end_epoch=T0 + 6.0)

    assert rec is not None and rec.path is not None
    assert len(encodes) == 1, "exactly one encode per recording"
    encoded_audio, encoded_path = encodes[0]
    assert encoded_path == rec.path
    # What was encoded is the TRIMMED audio, not the raw slice.
    assert pcm.seconds(len(encoded_audio)) < 5.0


@pytest.mark.asyncio
async def test_a_failed_encode_leaves_no_file_and_no_recording(recorder, monkeypatch):
    async def _fail(audio, path):
        return False

    monkeypatch.setattr(recorder_mod, "encode_mp3", _fail)
    ingest(recorder, T0, T0 + 5.0)
    await recorder.start_recording("call-1", start_epoch=T0 + 1.0)

    assert await recorder.stop_recording(end_epoch=T0 + 3.0) is None
    assert list(recorder._recordings_dir.glob("*.mp3")) == []


def test_encoder_command_contract_matches_what_c2_expects():
    """
    /upload has always received mono MP3 at 22050 Hz / 16 kbps, and Whisper
    consumes it downstream. The single encode must not quietly change that.
    """
    assert recorder_mod.MP3_SAMPLE_RATE == str(pcm.SAMPLE_RATE) == "22050"
    assert recorder_mod.MP3_BITRATE == "16k"


# ---------------------------------------------------------------------------
# Silence trimming and timing metadata
# ---------------------------------------------------------------------------

async def _recorded(recorder, lead_silence=1.0, voice=2.0, tail_silence=1.0):
    start = T0
    ingest(recorder, start, start + lead_silence, chunk=QUIET)
    ingest(recorder, start + lead_silence, start + lead_silence + voice, chunk=VOICE)
    ingest(recorder, start + lead_silence + voice,
           start + lead_silence + voice + tail_silence, chunk=QUIET)
    await recorder.start_recording("call-1", start_epoch=start + 0.5)
    return await recorder.stop_recording(end_epoch=start + lead_silence + voice + tail_silence)


@pytest.mark.asyncio
async def test_trimming_is_off_when_the_setting_is_off(recorder, monkeypatch):
    monkeypatch.setattr(settings, "trim_silence", False)
    called = False

    def _never(*args, **kwargs):
        nonlocal called
        called = True
        return b"", None

    monkeypatch.setattr(recorder_mod.audio_trim, "trim_pcm", _never)
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

    rec = await _recorded(recorder, lead_silence=1.0, voice=2.0, tail_silence=1.5)

    assert rec is not None and rec.path is not None
    assert rec.lead_trimmed > 0.0 and rec.tail_trimmed > 0.0
    guard = settings.trim_silence_guard_seconds
    # Slice began at T0+0.25; speech begins at T0+1.0, so the audio now starts
    # one guard margin before the speech.
    assert rec.audio_start_epoch == pytest.approx(T0 + 1.0 - guard, abs=3 * CHUNK_INTERVAL)
    assert rec.audio_end_epoch == pytest.approx(T0 + 3.0 + guard, abs=3 * CHUNK_INTERVAL)
    assert rec.audio_end_epoch > rec.audio_start_epoch


@pytest.mark.asyncio
async def test_all_silence_recording_is_dropped_and_logged(recorder, monkeypatch, caplog, encodes):
    monkeypatch.setattr(settings, "trim_silence", True)
    ingest(recorder, T0, T0 + 5.0, chunk=QUIET)
    await recorder.start_recording("call-1", start_epoch=T0 + 1.0)

    with caplog.at_level("WARNING", logger="drb-edge-node"):
        rec = await recorder.stop_recording(end_epoch=T0 + 4.0)

    assert rec is not None
    assert rec.all_silence is True
    assert rec.path is None, "an all-silence recording must not be uploaded"
    assert encodes == [], "and must not be encoded either"
    assert any("no speech" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Recording lifecycle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_second_start_is_rejected_while_recording(recorder):
    ingest(recorder, T0, T0 + 5.0)
    assert await recorder.start_recording("call-1", start_epoch=T0 + 1.0) is True
    assert await recorder.start_recording("call-2", start_epoch=T0 + 2.0) is False
    assert recorder.is_recording


@pytest.mark.asyncio
async def test_stop_without_start_is_a_noop(recorder):
    assert await recorder.stop_recording() is None
    assert not recorder.is_recording


@pytest.mark.asyncio
async def test_discard_drops_the_audio_without_writing_anything(recorder, encodes):
    """The orphan-audio path: unattributed audio must never reach a file."""
    ingest(recorder, T0, T0 + 5.0)
    await recorder.start_recording("call-orphan", start_epoch=T0 + 1.0)

    await recorder.discard_recording()

    assert not recorder.is_recording
    assert encodes == []
    assert list(recorder._recordings_dir.glob("*.mp3")) == []
    # ...and the recorder is immediately reusable.
    assert await recorder.start_recording("call-next", start_epoch=T0 + 2.0) is True


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

def test_capture_command_asks_for_raw_pcm_not_mp3(recorder):
    cmd = recorder._ffmpeg_command()
    joined = " ".join(cmd)

    assert "-f pulse" in joined
    assert "drb_sink.monitor" in joined, "must address the monitor explicitly, not 'default'"
    assert cmd[-1] == "-" and cmd[-2] == "s16le", "capture must emit raw PCM on stdout"
    assert "mp3" not in joined, "MP3 now happens once at save time, not in the capture"
    assert "-ar" in cmd and str(pcm.SAMPLE_RATE) in cmd
    assert "-ac" in cmd and str(pcm.CHANNELS) in cmd


def test_read_chunk_is_finer_than_the_pre_roll(recorder):
    """
    Chunk size is both the ring buffer's timestamp resolution and the window
    silence detection runs over, so it has to stay well under the pre-roll.
    """
    chunk_seconds = pcm.seconds(recorder_mod.READ_CHUNK_BYTES)
    assert chunk_seconds < PRE_ROLL_SECONDS / 4
    assert recorder_mod.READ_CHUNK_BYTES % pcm.FRAME_BYTES == 0


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
