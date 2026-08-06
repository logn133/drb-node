"""
Leading/trailing silence removal, as a slice of raw PCM.

WHY: P25 grants the channel, radios tune, and only then does a human start
talking; the recorder also deliberately over-captures at the tail (it closes a
call only after N seconds of silence have actually been HEARD). Both ends
therefore carry dead air. That is not just wasted Whisper spend: silence is a
well-documented trigger for Whisper hallucinating text that was never spoken,
and a hallucinated sentence poisons entity extraction and then incident
correlation downstream.

WHY IT IS SAFE: only the head and tail are touched, never the middle, and a
guard margin is kept around the detected speech so no syllable can be clipped.
If detection says the whole buffer is silent we do NOT emit a zero-length
recording — the caller is told and decides (see call_recorder: it skips the
upload and logs).

TIMING: trimming changes the audio's duration relative to the call's wall-clock
start/end, so every trim reports exactly how much was removed from each end.
Callers must carry those offsets forward — `started_at`/`ended_at` keep meaning
the CALL's bounds, and the trimmed audio's own bounds are reported separately.

HISTORY — THIS USED TO BE TWO FFMPEG PASSES. Detection was `silencedetect`
parsed out of FFmpeg's stderr, and the cut was a second FFmpeg re-encode. Both
are gone: the recorder now buffers PCM, so detection is arithmetic over the
samples and the cut is a byte-offset slice. Consequences worth keeping in mind:

  * The recording is encoded to MP3 exactly ONCE, after this runs, instead of
    being captured as MP3 and then re-encoded. One less generation of lossy
    encoding on every upload, and one less subprocess per call.
  * The threshold is now RMS over a short window (see pcm.rms_dbfs), where
    FFmpeg's silencedetect compared |sample| per sample. Same units (dBFS),
    slightly different meaning — do not port an old threshold across without
    re-reading the field logs.
  * There is no "is it worth re-encoding" minimum any more. A slice is free, so
    even a 0.05 s trim is applied.
"""
from dataclasses import dataclass
from typing import Optional, Tuple

from app.config import settings
from app.internal import pcm
from app.internal.logger import logger

# Window the head/tail scan works in. 20 ms is short enough that the guard
# margin below dwarfs the quantisation error, and long enough that RMS means
# something.
ANALYSIS_WINDOW_SECONDS = 0.02

# How far in from each end the scan is willing to look before giving up.
#
# Bounds the only unbounded cost in this module: the per-sample RMS loop. A
# normal recording resolves within a window or two at the head (the recorder
# starts on voice onset) and within the silence run at the tail, so this cap is
# never reached in practice. If it IS reached, we leave the audio untrimmed and
# say so — shipping an untrimmed recording is always better than shipping none.
MAX_SCAN_SECONDS = 30.0


@dataclass(frozen=True)
class TrimResult:
    """Outcome of a trim attempt. `lead`/`tail` are seconds actually removed."""

    lead: float = 0.0
    tail: float = 0.0
    duration_before: float = 0.0
    duration_after: float = 0.0
    all_silence: bool = False
    applied: bool = False
    # True when the scan hit MAX_SCAN_SECONDS without finding speech, so
    # `all_silence` could not be determined and nothing was trimmed.
    scan_truncated: bool = False

    @property
    def trimmed_seconds(self) -> float:
        return self.lead + self.tail


def _window_bytes() -> int:
    return max(pcm.FRAME_BYTES, pcm.byte_offset(ANALYSIS_WINDOW_SECONDS))


def first_signal_offset(
    audio: bytes,
    threshold_db: float,
    limit_seconds: float = MAX_SCAN_SECONDS,
) -> Optional[int]:
    """
    Byte offset of the first window carrying signal, scanning forward.

    None means "no signal found" — either the buffer really is all silence or
    the scan hit `limit_seconds` first; the caller distinguishes the two by
    comparing the scanned span against the buffer length.
    """
    window = _window_bytes()
    limit = min(len(audio), pcm.byte_offset(limit_seconds) or len(audio))
    offset = 0
    while offset < limit:
        chunk = audio[offset:offset + window]
        if not pcm.is_silent(chunk, threshold_db):
            return offset
        offset += window
    return None


def last_signal_offset(
    audio: bytes,
    threshold_db: float,
    limit_seconds: float = MAX_SCAN_SECONDS,
) -> Optional[int]:
    """
    Byte offset of the END of the last window carrying signal, scanning back.

    Returns the offset one past the last signal-bearing window, so it can be
    used directly as a slice bound.
    """
    window = _window_bytes()
    total = pcm.align(len(audio))
    floor = max(0, total - (pcm.byte_offset(limit_seconds) or total))
    offset = total
    while offset > floor:
        start = max(floor, offset - window)
        if not pcm.is_silent(audio[start:offset], threshold_db):
            return offset
        offset = start
    return None


def keep_window(
    first_signal: Optional[int],
    last_signal: Optional[int],
    total_bytes: int,
    guard_bytes: int,
) -> Tuple[int, int]:
    """
    Turn detected signal bounds into the byte range to keep.

    Pure and side-effect free so the decision that can destroy a transmission
    stays unit-testable without any audio. Offsets are sample-aligned and
    clamped to the buffer.
    """
    total = pcm.align(total_bytes)
    start = 0 if first_signal is None else max(0, first_signal - guard_bytes)
    end = total if last_signal is None else min(total, last_signal + guard_bytes)
    start = pcm.align(start)
    end = pcm.align(end)
    if end <= start:
        return 0, total
    return start, end


def trim_pcm(
    audio: bytes,
    threshold_db: Optional[float] = None,
    guard: Optional[float] = None,
) -> Tuple[bytes, TrimResult]:
    """
    Return (kept_audio, result). Never raises and never returns empty audio.

    An all-silence buffer is returned UNCHANGED with `all_silence=True`: the
    caller decides what to do with a recording that contains no speech at all —
    that is itself a signal (squelch misconfigured, wrong sink, dead audio
    path), not something to silently truncate to nothing.
    """
    threshold = settings.trim_silence_threshold_db if threshold_db is None else threshold_db
    margin = settings.trim_silence_guard_seconds if guard is None else guard

    total = pcm.align(len(audio))
    duration = pcm.seconds(total)
    if total <= 0:
        return audio, TrimResult()

    first = first_signal_offset(audio, threshold)
    if first is None:
        scanned = min(total, pcm.byte_offset(MAX_SCAN_SECONDS) or total)
        if scanned < total:
            # Could not prove it is all silence; refuse to guess.
            logger.warning(
                f"Silence scan gave up after {MAX_SCAN_SECONDS:.0f}s without finding speech in a "
                f"{duration:.1f}s recording — leaving it untrimmed."
            )
            return audio, TrimResult(
                duration_before=duration, duration_after=duration, scan_truncated=True
            )
        logger.warning(
            f"Recording is entirely silence ({duration:.2f}s, threshold {threshold:.1f}dBFS RMS) — "
            "no speech detected."
        )
        return audio, TrimResult(duration_before=duration, duration_after=duration, all_silence=True)

    last = last_signal_offset(audio, threshold)
    guard_bytes = pcm.byte_offset(margin)
    keep_start, keep_end = keep_window(first, last, total, guard_bytes)

    lead = pcm.seconds(keep_start)
    tail = pcm.seconds(total - keep_end)
    if keep_start <= 0 and keep_end >= total:
        return audio[:total], TrimResult(duration_before=duration, duration_after=duration)

    kept = audio[keep_start:keep_end]
    after = pcm.seconds(len(kept))
    logger.info(
        f"Trimmed recording: -{lead:.2f}s lead, -{tail:.2f}s tail "
        f"({duration:.2f}s -> {after:.2f}s, threshold {threshold:.1f}dBFS RMS)"
    )
    return kept, TrimResult(
        lead=lead,
        tail=tail,
        duration_before=duration,
        duration_after=after,
        applied=True,
    )
