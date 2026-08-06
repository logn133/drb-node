"""
Unit tests for the raw-PCM primitives that silence detection rests on.

The whole audio-driven design depends on one field observation: between
transmissions the captured stream is DIGITAL silence (a PulseAudio null-sink
monitor), measured at about -91 dBFS — one least-significant bit — not an analog
noise floor. These tests pin that assumption down in code: a 1-LSB "silent"
buffer must read as silence at every sane threshold, and speech-level audio must
never read as silence.
"""
from array import array

import pytest

from app.internal import pcm


def tone(level: int, samples: int = 1024) -> bytes:
    """A square wave at +/-level, so RMS == level exactly."""
    return array("h", [level, -level] * (samples // 2)).tobytes()


def zeros(samples: int = 1024) -> bytes:
    return b"\x00\x00" * samples


# ---------------------------------------------------------------------------
# Format arithmetic
# ---------------------------------------------------------------------------

def test_capture_format_is_22050_mono_16bit():
    """MP3_SAMPLE_RATE in call_recorder must match, or the encode resamples."""
    assert (pcm.SAMPLE_RATE, pcm.CHANNELS, pcm.SAMPLE_WIDTH) == (22050, 1, 2)
    assert pcm.BYTES_PER_SECOND == 44100


def test_seconds_and_byte_offset_round_trip():
    assert pcm.seconds(pcm.BYTES_PER_SECOND) == pytest.approx(1.0)
    assert pcm.byte_offset(1.0) == pcm.BYTES_PER_SECOND
    assert pcm.byte_offset(0.5) == 22050


def test_byte_offset_is_always_sample_aligned():
    """A byte offset that splits a sample would shift every later sample."""
    for seconds in (0.001, 0.0137, 0.25, 1.7):
        assert pcm.byte_offset(seconds) % pcm.FRAME_BYTES == 0


def test_align_drops_a_trailing_half_sample():
    assert pcm.align(9) == 8
    assert pcm.align(0) == 0
    assert pcm.align(-4) == 0


# ---------------------------------------------------------------------------
# Silence detection
# ---------------------------------------------------------------------------

def test_exact_digital_zero_is_silence():
    assert pcm.is_all_zero(zeros())
    assert pcm.rms_dbfs(zeros()) == pcm.SILENT_DBFS
    assert pcm.is_silent(zeros(), -50.0)
    assert pcm.is_silent(zeros(), -90.0)


def test_one_lsb_of_dither_is_the_measured_field_floor():
    """
    The gap between transmissions measures ~-91 dBFS on a live node, which is
    exactly 20*log10(1/32768) — a single LSB. It must read as silence at any
    threshold we would ever configure.
    """
    floor = tone(1)
    assert pcm.rms_dbfs(floor) == pytest.approx(-90.3, abs=0.2)
    assert pcm.is_silent(floor, -50.0)
    assert pcm.is_silent(floor, -70.0)
    assert not pcm.is_silent(floor, -95.0), "an absurd threshold should still be honoured"


def test_speech_level_audio_is_never_silence():
    """Speech on the live node averages about -18 dBFS."""
    speech = tone(4096)  # -18.06 dBFS
    assert pcm.rms_dbfs(speech) == pytest.approx(-18.06, abs=0.1)
    assert not pcm.is_silent(speech, -50.0)
    assert not pcm.is_silent(speech, -40.0)


def test_threshold_is_honoured_exactly_at_the_boundary():
    # RMS 104 -> -49.96 dBFS, just above a -50 threshold.
    assert not pcm.is_silent(tone(104), -50.0)
    # RMS 100 -> -50.30 dBFS, just below it.
    assert pcm.is_silent(tone(100), -50.0)


def test_empty_buffer_counts_as_silence():
    """
    "No audio arrived" must never read as "someone is talking" — otherwise a
    stalled capture would hold a segment open forever.
    """
    assert pcm.is_silent(b"", -50.0)
    assert pcm.rms_dbfs(b"") == pcm.SILENT_DBFS


def test_full_scale_is_zero_dbfs():
    assert pcm.rms_dbfs(tone(32767)) == pytest.approx(0.0, abs=0.001)


def test_a_trailing_odd_byte_does_not_break_detection():
    """Short reads at EOF can leave half a sample; it must be dropped, not skew."""
    assert not pcm.is_silent(tone(8000) + b"\x00", -50.0)
    assert pcm.samples(zeros(4) + b"\x01").itemsize == 2
    assert len(pcm.samples(zeros(4) + b"\x01")) == 4


def test_rms_attenuates_an_isolated_click_the_way_peak_would_not():
    """
    Why RMS and not peak. A single stray sample in an otherwise silent window is
    a decoder click, not speech. Peak would score it at its full amplitude and
    hold a recording open; RMS spreads it over the window and divides it down by
    sqrt(N) — 30 dB for a 1024-sample window.

    A full-scale click still reads as signal even after that attenuation, which
    is deliberate: at worst it extends a recording by the silence timeout, and
    the trim strips the result before upload. Under-detecting speech is the
    failure that loses words permanently.
    """
    moderate = bytearray(zeros(1024))
    moderate[0:2] = array("h", [1000]).tobytes()  # -30 dBFS peak
    assert pcm.rms_dbfs(bytes(moderate)) == pytest.approx(-60.4, abs=0.2)
    assert pcm.is_silent(bytes(moderate), -50.0)

    full_scale = bytearray(zeros(1024))
    full_scale[0:2] = array("h", [32767]).tobytes()
    assert pcm.rms_dbfs(bytes(full_scale)) == pytest.approx(-30.1, abs=0.2)
    assert not pcm.is_silent(bytes(full_scale), -50.0)
