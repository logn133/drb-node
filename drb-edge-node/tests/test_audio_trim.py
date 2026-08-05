"""
Unit tests for silence-trim decision logic.

`speech_bounds` is pure on purpose so the "what do we keep" decision — the part
that can destroy a transmission if it is wrong — is testable without FFmpeg.
The numbers below come from ffmpeg silencedetect run against six real recordings
off a live P25 node: 1.71–2.45 s of leading silence and 0.00–1.11 s trailing.
"""
import pytest

from app.internal.audio_trim import (
    TrimResult,
    _parse_duration,
    _parse_silences,
    speech_bounds,
)

GUARD = 0.25


def test_leading_silence_is_trimmed_with_a_guard_margin():
    # Real shape of file f4bfaa1f: 1.85s lead, 0.34s trail, 4.54s total.
    regions = [(0.0, 1.85), (4.20, None)]
    start, end, all_silence = speech_bounds(regions, duration=4.54, guard=GUARD)

    assert not all_silence
    assert start == pytest.approx(1.85 - GUARD)
    assert end == pytest.approx(4.20 + GUARD)
    # The guard must never eat into detected speech.
    assert start < 1.85 and end > 4.20


def test_guard_margin_never_runs_past_the_file_bounds():
    regions = [(0.0, 0.10), (3.95, None)]
    start, end, _ = speech_bounds(regions, duration=4.0, guard=1.0)

    assert start == 0.0
    assert end == 4.0


def test_trailing_silence_is_trimmed_when_ffmpeg_closes_the_region_at_eof():
    """
    FFmpeg 6.x flushes a `silence_end` at EOF, so a trailing region looks closed.
    Treating "no silence_end" as the only trailing signal silently disabled tail
    trimming entirely — verified against ffmpeg 6.1.1.
    """
    # Real ffmpeg 6.1.1 output for a 2s-silence + 1.5s-tone + 1s-silence file.
    regions = [(0.0, 2.05361), (3.56367, 4.63102)]
    start, end, all_silence = speech_bounds(regions, duration=4.65, guard=GUARD)

    assert not all_silence
    assert start == pytest.approx(2.05361 - GUARD)
    assert end == pytest.approx(3.56367 + GUARD), "the trailing second must be trimmed"


def test_all_silence_survives_ffmpeg_closing_the_region_at_eof():
    # Real ffmpeg 6.1.1 output for a 4s file of pure silence.
    _, _, all_silence = speech_bounds([(0.0, 4.0)], duration=4.03, guard=GUARD)
    assert all_silence


def test_trailing_silence_that_does_not_reach_eof_is_left_alone():
    """
    A silence region with a closing silence_end is an internal pause between
    transmissions, not dead air at the tail. Trimming it would cut the middle
    out of a conversation.
    """
    regions = [(0.0, 1.9), (5.0, 7.5)]
    start, end, _ = speech_bounds(regions, duration=12.0, guard=GUARD)

    assert start == pytest.approx(1.9 - GUARD)
    assert end == 12.0, "an internal pause must not shorten the file"


def test_no_silence_detected_keeps_the_whole_file():
    start, end, all_silence = speech_bounds([], duration=6.0, guard=GUARD)

    assert (start, end) == (0.0, 6.0)
    assert not all_silence


def test_silence_starting_late_is_not_treated_as_leading():
    """Only a region at the very head counts as leading silence."""
    regions = [(1.20, 2.00)]
    start, end, _ = speech_bounds(regions, duration=5.0, guard=GUARD)

    assert start == 0.0, "speech before 1.20s must not be trimmed away"
    assert end == 5.0


def test_all_silence_is_reported_not_trimmed_to_nothing():
    # One region covering the whole file and running to EOF.
    regions = [(0.0, None)]
    start, end, all_silence = speech_bounds(regions, duration=4.0, guard=GUARD)

    assert all_silence
    assert (start, end) == (0.0, 4.0), "an all-silence file must not become zero-length"


def test_all_silence_when_head_and_tail_regions_overlap():
    regions = [(0.0, 3.2), (3.0, None)]
    _, _, all_silence = speech_bounds(regions, duration=4.0, guard=GUARD)

    assert all_silence


# ---------------------------------------------------------------------------
# FFmpeg output parsing
# ---------------------------------------------------------------------------

# Verbatim shape of ffmpeg 6.1.1 output.
FFMPEG_STDERR = """
Input #0, mp3, from '/recordings/x.mp3':
  Duration: 00:00:04.70, start: 0.050113, bitrate: 16 kb/s
[silencedetect @ 0000029160e63f40] silence_start: 0
[silencedetect @ 0000029160e63f40] silence_end: 2.05361 | silence_duration: 2.05361
[silencedetect @ 0000029160e63f40] silence_start: 3.56367
[silencedetect @ 0000029160e63f40] silence_end: 4.63102 | silence_duration: 1.06735
[out#0/null @ 0x2] video:0kB audio:97kB
"""

FFMPEG_STDERR_OPEN_TAIL = """
  Duration: 00:00:04.54, start: 0.000000, bitrate: 16 kb/s
[silencedetect @ 0x1] silence_start: 0
[silencedetect @ 0x1] silence_end: 1.85042 | silence_duration: 1.85042
[silencedetect @ 0x1] silence_start: 4.20134
"""


def test_duration_is_corrected_for_the_mp3_container_start_offset():
    """
    MP3 encoder delay makes the container duration longer than the audio
    silencedetect timestamps. Without this correction the trailing-region test
    needs a slack epsilon big enough to clip real speech.
    """
    assert _parse_duration(FFMPEG_STDERR) == pytest.approx(4.70 - 0.050113)


def test_duration_is_none_when_absent():
    assert _parse_duration("no duration here") is None


def test_silence_regions_are_parsed():
    regions = _parse_silences(FFMPEG_STDERR)

    assert len(regions) == 2
    assert regions[0] == (pytest.approx(0.0), pytest.approx(2.05361))
    assert regions[1] == (pytest.approx(3.56367), pytest.approx(4.63102))


def test_a_region_with_no_silence_end_is_still_parsed():
    """Older FFmpeg simply stopped reporting at EOF — keep handling that."""
    regions = _parse_silences(FFMPEG_STDERR_OPEN_TAIL)

    assert regions[-1][1] is None


def test_trim_result_reports_total_trimmed():
    result = TrimResult(path=None, lead=1.9, tail=0.35)
    assert result.trimmed_seconds == pytest.approx(2.25)
