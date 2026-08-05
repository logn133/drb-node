"""
Conservative leading/trailing silence removal for finished call recordings.

WHY: P25 grants the channel, radios tune, and only then does a human start
talking. Measured on six real recordings from a live node, that costs 1.71–2.45 s
of dead air at the head of every single file, and the tail pad adds its own
~0.3–1.0 s. Roughly 63% of every uploaded MP3 was silence. That is not just
wasted Whisper spend: silence is a well-documented trigger for Whisper
hallucinating text that was never spoken, and a hallucinated sentence poisons
entity extraction and then incident correlation downstream.

WHY IT IS SAFE: only the head and tail are touched, never the middle, and a
guard margin is kept around the detected speech so no syllable can be clipped.
If detection says the whole file is silent we do NOT emit a zero-length file —
the caller is told and decides (see call_recorder: it skips the upload and logs).

TIMING: trimming changes the audio's duration relative to the call's wall-clock
start/end, so every trim reports exactly how much was removed from each end.
Callers must carry those offsets forward — `started_at`/`ended_at` keep meaning
the CALL's bounds, and the trimmed audio's own bounds are reported separately.

Implementation is two FFmpeg passes (detect, then cut). FFmpeg is already a hard
dependency of this container and is already running the capture, so this adds no
new moving parts. `silenceremove` in one pass was rejected on purpose: it gives
no way to learn how much it removed, which would make the timing metadata above
impossible to produce.
"""
import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from app.config import settings
from app.internal.logger import logger

# Minimum run of quiet before FFmpeg calls it a silence region. Below ~0.3 s this
# starts firing on the natural pauses between words, which is not what we want —
# we only care about the big block of dead air at each end.
MIN_SILENCE_SECONDS = 0.3

# A silence region only counts as "leading" if it begins essentially at the file
# head. One chunk of slop.
HEAD_EPSILON_SECONDS = 0.15

# ...and only as "trailing" if it reaches the end of the audio. Do NOT assume a
# missing `silence_end` marks that case: FFmpeg 6.x flushes a closing
# `silence_end` at EOF, so an all-silence file looks exactly like a file with one
# closed silence region. Verified against ffmpeg 6.1.1 — the reported end lands
# ~0.03 s short of the (offset-corrected) duration, hence this epsilon. The
# residual risk is clipping <=0.1 s of audio that follows a >=0.3 s gap right at
# EOF, which the guard margin below more than covers.
TAIL_EPSILON_SECONDS = 0.10

# Don't bother re-encoding to reclaim less than this — a re-encode costs a CPU
# spike on a Pi and a generation of MP3 quality, which is a bad trade for
# a fraction of a second.
MIN_TRIM_SECONDS = 0.20

# Bounded so a wedged FFmpeg can never stall the upload path.
FFMPEG_TIMEOUT_SECONDS = 30.0

_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?[0-9.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(-?[0-9.]+)")
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d\d):(\d\d\.\d+)")
_START_RE = re.compile(r"Duration:[^\n]*?start:\s*(-?[0-9.]+)")


@dataclass(frozen=True)
class TrimResult:
    """Outcome of a trim attempt. `lead`/`tail` are seconds actually removed."""

    path: Optional[Path]
    lead: float = 0.0
    tail: float = 0.0
    duration_before: float = 0.0
    duration_after: float = 0.0
    all_silence: bool = False
    applied: bool = False

    @property
    def trimmed_seconds(self) -> float:
        return self.lead + self.tail


async def _run(cmd: List[str]) -> Tuple[int, str]:
    """Run FFmpeg and return (returncode, stderr). FFmpeg reports on stderr."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=FFMPEG_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        raise
    return proc.returncode or 0, stderr.decode(errors="replace")


def _parse_duration(stderr: str) -> Optional[float]:
    """
    Length of the decodable audio, in silencedetect's coordinates.

    MP3 carries encoder delay/padding, which FFmpeg reports as a container
    `start:` offset — the container duration is that much longer than the audio
    silencedetect actually timestamps. Subtracting it is what lets
    TAIL_EPSILON_SECONDS stay tight enough to be safe.
    """
    match = _DURATION_RE.search(stderr)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)

    start = _START_RE.search(stderr)
    if start:
        offset = float(start.group(1))
        if 0.0 <= offset < duration:
            duration -= offset
    return duration


def _parse_silences(stderr: str) -> List[Tuple[float, Optional[float]]]:
    """
    Extract silence regions as (start, end) with end=None when it runs to EOF.

    FFmpeg emits `silence_start:` and a later `silence_end:` per region, and
    simply never emits the closing line for a region that reaches EOF.
    """
    regions: List[Tuple[float, Optional[float]]] = []
    pending: Optional[float] = None
    for line in stderr.splitlines():
        if "silencedetect" not in line:
            continue
        start = _SILENCE_START_RE.search(line)
        if start:
            pending = float(start.group(1))
            continue
        end = _SILENCE_END_RE.search(line)
        if end and pending is not None:
            regions.append((pending, float(end.group(1))))
            pending = None
    if pending is not None:
        regions.append((pending, None))
    return regions


def speech_bounds(
    regions: List[Tuple[float, Optional[float]]],
    duration: float,
    guard: float,
) -> Tuple[float, float, bool]:
    """
    Turn detected silence regions into the [start, end] window to keep.

    Pure and side-effect free so the decision logic is unit-testable without
    FFmpeg. Returns (start, end, all_silence).
    """
    speech_start = 0.0
    speech_end = duration

    if regions:
        head_start, head_end = regions[0]
        if head_start <= HEAD_EPSILON_SECONDS and head_end is not None:
            speech_start = head_end

        tail_start, tail_end = regions[-1]
        # `None` = pre-6.x FFmpeg, which simply stopped reporting at EOF.
        reaches_eof = tail_end is None or (duration - tail_end) <= TAIL_EPSILON_SECONDS
        if reaches_eof:
            speech_end = min(speech_end, tail_start)

    if speech_end <= speech_start:
        # Detection says there is no speech anywhere in the file.
        return 0.0, duration, True

    # Guard margin: never trim right up against the first/last syllable.
    keep_start = max(0.0, speech_start - guard)
    keep_end = min(duration, speech_end + guard)
    return keep_start, keep_end, False


async def trim_silence(
    path: Path,
    sample_rate: str,
    bitrate: str,
    threshold_db: Optional[float] = None,
    guard: Optional[float] = None,
) -> TrimResult:
    """
    Trim leading/trailing silence in place, preserving the original on failure.

    Never raises: any problem degrades to "leave the file exactly as it was",
    because shipping an untrimmed recording is far better than shipping none.
    """
    threshold = settings.trim_silence_threshold_db if threshold_db is None else threshold_db
    margin = settings.trim_silence_guard_seconds if guard is None else guard

    try:
        _, stderr = await _run([
            "ffmpeg", "-hide_banner", "-nostdin", "-nostats",
            "-i", str(path),
            "-af", f"silencedetect=noise={threshold}dB:d={MIN_SILENCE_SECONDS}",
            "-f", "null", "-",
        ])
    except Exception as e:
        logger.warning(f"Silence detection failed for {path.name} ({e}) — uploading untrimmed.")
        return TrimResult(path=path)

    duration = _parse_duration(stderr)
    if duration is None or duration <= 0:
        logger.warning(f"Could not determine duration of {path.name} — uploading untrimmed.")
        return TrimResult(path=path)

    regions = _parse_silences(stderr)
    keep_start, keep_end, all_silence = speech_bounds(regions, duration, margin)

    if all_silence:
        # Deliberately NOT trimmed to nothing. The caller decides what to do with
        # a recording that contains no speech at all — that is itself a signal
        # (squelch misconfigured, wrong sink, dead audio path).
        logger.warning(
            f"{path.name} is entirely silence ({duration:.2f}s, threshold {threshold}dB) — "
            "no speech detected."
        )
        return TrimResult(path=path, duration_before=duration, duration_after=duration, all_silence=True)

    lead = keep_start
    tail = duration - keep_end
    if (lead + tail) < MIN_TRIM_SECONDS:
        return TrimResult(path=path, duration_before=duration, duration_after=duration)

    trimmed = path.with_name(f"{path.stem}_trimmed{path.suffix}")
    try:
        code, err = await _run([
            "ffmpeg", "-hide_banner", "-nostdin", "-nostats",
            "-loglevel", "warning", "-y",
            "-ss", f"{keep_start:.3f}",
            "-i", str(path),
            "-t", f"{keep_end - keep_start:.3f}",
            "-ac", "1", "-ar", sample_rate, "-b:a", bitrate,
            "-f", "mp3", str(trimmed),
        ])
    except Exception as e:
        logger.warning(f"Silence trim failed for {path.name} ({e}) — uploading untrimmed.")
        trimmed.unlink(missing_ok=True)
        return TrimResult(path=path, duration_before=duration, duration_after=duration)

    if code != 0 or not trimmed.exists() or trimmed.stat().st_size == 0:
        logger.warning(f"Silence trim produced no output for {path.name} ({err.strip()}) — uploading untrimmed.")
        trimmed.unlink(missing_ok=True)
        return TrimResult(path=path, duration_before=duration, duration_after=duration)

    trimmed.replace(path)
    logger.info(
        f"Trimmed {path.name}: -{lead:.2f}s lead, -{tail:.2f}s tail "
        f"({duration:.2f}s → {keep_end - keep_start:.2f}s)"
    )
    return TrimResult(
        path=path,
        lead=lead,
        tail=tail,
        duration_before=duration,
        duration_after=keep_end - keep_start,
        applied=True,
    )
