"""
Unit tests for silence trimming, now a byte-offset slice of raw PCM.

`keep_window` is pure on purpose so the "what do we keep" decision — the part
that can destroy a transmission if it is wrong — stays testable without any
audio at all. The rest of the file drives the real detector over synthesised
buffers shaped like the six real recordings measured off a live P25 node:
1.71-2.45 s of leading silence and 0.00-1.11 s trailing.

The old implementation shelled out to FFmpeg twice (silencedetect, then a
re-encode) and these tests parsed its stderr. Both passes are gone; the recorder
buffers PCM, so detection is arithmetic and the cut is a slice.
"""
from array import array

import pytest

from app.config import settings
from app.internal import audio_trim, pcm
from app.internal.audio_trim import (
    TrimResult,
    first_signal_offset,
    keep_window,
    last_signal_offset,
    trim_pcm,
)

GUARD = 0.25
SPEECH_LEVEL = 4096   # -18 dBFS, the measured field average
FLOOR_LEVEL = 1       # -90.3 dBFS, the measured digital-silence floor


def speech(seconds: float) -> bytes:
    count = int(pcm.SAMPLE_RATE * seconds)
    return array("h", [SPEECH_LEVEL, -SPEECH_LEVEL] * (count // 2)).tobytes()


def silence(seconds: float, level: int = FLOOR_LEVEL) -> bytes:
    count = int(pcm.SAMPLE_RATE * seconds)
    return array("h", [level, -level] * (count // 2)).tobytes()


# ---------------------------------------------------------------------------
# keep_window — the pure decision
# ---------------------------------------------------------------------------

def test_guard_margin_is_kept_around_detected_speech():
    guard = pcm.byte_offset(GUARD)
    start, end = keep_window(
        first_signal=pcm.byte_offset(1.85),
        last_signal=pcm.byte_offset(4.20),
        total_bytes=pcm.byte_offset(4.54),
        guard_bytes=guard,
    )
    assert pcm.seconds(start) == pytest.approx(1.85 - GUARD, abs=0.001)
    assert pcm.seconds(end) == pytest.approx(4.20 + GUARD, abs=0.001)


def test_guard_margin_never_runs_past_the_buffer_bounds():
    total = pcm.byte_offset(4.0)
    start, end = keep_window(
        first_signal=pcm.byte_offset(0.10),
        last_signal=pcm.byte_offset(3.95),
        total_bytes=total,
        guard_bytes=pcm.byte_offset(1.0),
    )
    assert (start, end) == (0, total)


def test_keep_window_offsets_are_sample_aligned():
    start, end = keep_window(3, 9, 21, 1)
    assert start % pcm.FRAME_BYTES == 0
    assert end % pcm.FRAME_BYTES == 0


def test_no_signal_found_keeps_everything():
    total = pcm.byte_offset(6.0)
    assert keep_window(None, None, total, pcm.byte_offset(GUARD)) == (0, total)


def test_an_inverted_window_degrades_to_keeping_everything():
    """Never return an empty slice, whatever the inputs say."""
    total = pcm.byte_offset(4.0)
    assert keep_window(pcm.byte_offset(3.0), pcm.byte_offset(0.5), total, 0) == (0, total)


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def test_first_and_last_signal_are_found_in_a_realistic_recording():
    audio = silence(1.85) + speech(2.35) + silence(0.34)
    threshold = -40.0

    first = first_signal_offset(audio, threshold)
    last = last_signal_offset(audio, threshold)

    assert pcm.seconds(first) == pytest.approx(1.85, abs=audio_trim.ANALYSIS_WINDOW_SECONDS)
    assert pcm.seconds(last) == pytest.approx(4.20, abs=audio_trim.ANALYSIS_WINDOW_SECONDS)


def test_internal_pauses_are_not_treated_as_the_tail():
    """Trimming the middle out of a conversation would be unrecoverable."""
    audio = silence(1.9) + speech(3.1) + silence(2.5) + speech(4.5)
    last = last_signal_offset(audio, -40.0)
    assert pcm.seconds(last) == pytest.approx(12.0, abs=audio_trim.ANALYSIS_WINDOW_SECONDS)


def test_all_silence_returns_no_signal_offset():
    assert first_signal_offset(silence(4.0), -40.0) is None
    assert last_signal_offset(silence(4.0), -40.0) is None


def test_the_scan_is_bounded_so_a_long_buffer_cannot_stall_the_upload():
    """The per-sample loop is the only unbounded cost; it must have a ceiling."""
    audio = silence(2.0)
    assert first_signal_offset(audio, -40.0, limit_seconds=0.5) is None
    assert first_signal_offset(speech(0.1) + silence(1.9), -40.0, limit_seconds=0.5) == 0


# ---------------------------------------------------------------------------
# trim_pcm — end to end over synthesised audio
# ---------------------------------------------------------------------------

def test_leading_and_trailing_silence_are_trimmed_to_the_guard_margin():
    audio = silence(1.85) + speech(2.35) + silence(0.34)
    kept, result = trim_pcm(audio, threshold_db=-40.0, guard=GUARD)

    assert result.applied and not result.all_silence
    assert result.lead == pytest.approx(1.85 - GUARD, abs=0.05)
    assert result.tail == pytest.approx(0.34 - GUARD, abs=0.05)
    assert pcm.seconds(len(kept)) == pytest.approx(result.duration_after, abs=0.001)
    assert result.duration_after < result.duration_before


def test_the_guard_margin_never_eats_into_speech():
    audio = silence(2.0) + speech(1.0) + silence(2.0)
    kept, result = trim_pcm(audio, threshold_db=-40.0, guard=GUARD)

    # Everything removed from the head must be silence, and the first sample of
    # real speech must survive.
    assert result.lead < 2.0
    assert pcm.seconds(len(kept)) > 1.0


def test_measured_trailing_silence_is_reported_for_field_tuning():
    """
    The recorder deliberately over-captures the tail (it closes only after the
    silence timeout has actually elapsed in the audio), so `tail` is how the
    real silence run reaches the logs.
    """
    audio = silence(0.5) + speech(2.0) + silence(3.0)
    _, result = trim_pcm(audio, threshold_db=-40.0, guard=GUARD)
    assert result.tail == pytest.approx(3.0 - GUARD, abs=0.05)
    assert result.trimmed_seconds == pytest.approx(result.lead + result.tail)


def test_an_all_silence_buffer_is_reported_not_truncated_to_nothing():
    audio = silence(4.0)
    kept, result = trim_pcm(audio, threshold_db=-40.0, guard=GUARD)

    assert result.all_silence
    assert not result.applied
    assert kept == audio, "an all-silence recording must not become zero-length"


def test_digital_silence_at_the_measured_field_floor_is_detected():
    """
    The -91 dBFS floor is the whole reason this needs no field calibration.
    Detection must not depend on the threshold being tuned to a noise floor.
    """
    audio = silence(1.0, level=1) + speech(1.0) + silence(1.0, level=1)
    for threshold in (-70.0, -60.0, -50.0, -40.0):
        _, result = trim_pcm(audio, threshold_db=threshold, guard=GUARD)
        assert result.applied, f"threshold {threshold} should still find the speech"
        assert result.lead == pytest.approx(0.75, abs=0.05)


def test_audio_with_no_silence_at_either_end_is_left_alone():
    audio = speech(3.0)
    kept, result = trim_pcm(audio, threshold_db=-40.0, guard=GUARD)

    assert not result.applied
    assert kept == audio
    assert result.duration_before == pytest.approx(result.duration_after)


def test_an_empty_buffer_is_handled():
    kept, result = trim_pcm(b"", threshold_db=-40.0, guard=GUARD)
    assert kept == b"" and not result.applied and not result.all_silence


def test_thresholds_default_to_settings():
    audio = silence(1.0) + speech(1.0) + silence(1.0)
    _, result = trim_pcm(audio)
    assert result.applied
    assert settings.trim_silence_threshold_db == -40.0
    assert settings.trim_silence_guard_seconds == 0.25
    assert result.lead == pytest.approx(1.0 - settings.trim_silence_guard_seconds, abs=0.05)


def test_a_scan_that_gives_up_leaves_the_audio_untouched_and_says_so():
    """
    Refusing to guess is the point: an untrimmed upload is always better than a
    wrongly-truncated one, and better than dropping a call as "all silence"
    without having actually looked at all of it.
    """
    long_silence = silence(audio_trim.MAX_SCAN_SECONDS + 5.0)
    kept, result = trim_pcm(long_silence, threshold_db=-40.0, guard=GUARD)

    assert result.scan_truncated
    assert not result.all_silence
    assert not result.applied
    assert kept == long_silence


def test_trim_result_reports_total_trimmed():
    assert TrimResult(lead=1.9, tail=0.35).trimmed_seconds == pytest.approx(2.25)
