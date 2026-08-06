"""
Raw PCM primitives: the one place that knows the capture format.

The capture pipeline buffers RAW PCM (signed 16-bit little-endian, mono,
22050 Hz) instead of MP3. Three things fall out of that, and they are the whole
reason for the change:

  1. Silence detection is integer arithmetic over the bytes as they arrive —
     no decode, no FFmpeg, no second process. That is what makes an
     AUDIO-DRIVEN call boundary possible at all.
  2. Trimming becomes a byte-offset slice instead of a second encode pass.
  3. MP3 encoding happens exactly ONCE, at save time, so uploads stop being
     double-encoded.

WHY SILENCE IS UNAMBIGUOUS HERE: between transmissions the captured stream is
the monitor of a PulseAudio *null sink*, which emits digital silence, not an
analog noise floor. Measured on a live node, the gap between transmissions sits
at about -91 dBFS — that is 20*log10(1/32768), i.e. one least-significant bit,
the quietest thing a 16-bit sample can be without being exactly zero. Speech on
the same node averages about -18 dBFS. There is therefore ~70 dB of daylight
between "silence" and "voice", and the threshold does NOT need field
calibration against radio noise the way an analog squelch tail would.

MEASUREMENT IS RMS, NOT PEAK. Peak would be cheaper but a single decoder click
would read as voice for a whole window; RMS over a window is the honest
"is there signal here" answer. The cost is a Python loop over the window's
samples, which is affordable because of how little audio is ever scanned:
one ~46 ms chunk per chunk arrival at capture time, and only the head/tail of a
finished recording at trim time (see audio_trim.MAX_SCAN_SECONDS). A cheap
all-zero fast path in C skips the loop entirely for exactly-silent windows.

BYTE ORDER: FFmpeg is asked for s16le. `array("h")` is native-endian, so on a
big-endian host the samples are byte-swapped before use. Every DRB target is
little-endian today; this is three lines of insurance, not a real scenario.
"""
import math
import sys
from array import array
from typing import Union

# Capture format. MP3_SAMPLE_RATE in call_recorder must stay equal to
# SAMPLE_RATE — the encode at save time is a straight pass with no resample.
SAMPLE_RATE = 22050
SAMPLE_WIDTH = 2
CHANNELS = 1
FRAME_BYTES = SAMPLE_WIDTH * CHANNELS
BYTES_PER_SECOND = SAMPLE_RATE * FRAME_BYTES  # 44100 B/s

# 16-bit full scale. A sample of 32768 (or -32768) is 0 dBFS.
FULL_SCALE = 32768.0

# Reported for a window with no signal at all. Any real threshold is far above
# this, so it always compares as "silent" without special-casing log10(0).
SILENT_DBFS = -120.0

_NEEDS_BYTESWAP = sys.byteorder != "little"

Buffer = Union[bytes, bytearray]


def align(nbytes: int) -> int:
    """Round a byte count DOWN to a whole number of samples."""
    if nbytes <= 0:
        return 0
    return nbytes - (nbytes % FRAME_BYTES)


def seconds(nbytes: int) -> float:
    """Duration of `nbytes` of PCM."""
    return nbytes / BYTES_PER_SECOND


def byte_offset(sec: float) -> int:
    """Sample-aligned byte offset of `sec` seconds into a PCM buffer."""
    return align(int(sec * BYTES_PER_SECOND))


def samples(buf: Buffer) -> array:
    """View a PCM buffer as signed 16-bit samples, dropping any partial frame."""
    usable = align(len(buf))
    data = array("h")
    if usable:
        data.frombytes(bytes(buf[:usable]))
        if _NEEDS_BYTESWAP:
            data.byteswap()
    return data


def is_all_zero(buf: Buffer) -> bool:
    """
    True when every byte is zero — exact digital silence.

    `bytes.count` runs in C, so this is the cheap path that lets a long scan
    over silence stay fast without touching the per-sample loop below.
    """
    return len(buf) > 0 and buf.count(0) == len(buf)


def rms(buf: Buffer) -> float:
    """Root-mean-square amplitude in raw sample units (0 .. 32768)."""
    data = samples(buf)
    if not data:
        return 0.0
    total = 0
    for sample in data:
        total += sample * sample
    return math.sqrt(total / len(data))


def rms_dbfs(buf: Buffer) -> float:
    """RMS level of a PCM window in dBFS. SILENT_DBFS for an empty/zero window."""
    if not buf or is_all_zero(buf):
        return SILENT_DBFS
    value = rms(buf)
    if value <= 0.0:
        return SILENT_DBFS
    return 20.0 * math.log10(min(value, FULL_SCALE) / FULL_SCALE)


def is_silent(buf: Buffer, threshold_db: float) -> bool:
    """
    True when a PCM window carries no signal above `threshold_db` (dBFS RMS).

    An empty buffer counts as silence: "no audio arrived" must never read as
    "someone is talking", or a stalled capture would hold a segment open.
    """
    if not buf:
        return True
    if is_all_zero(buf):
        return True
    return rms_dbfs(buf) < threshold_db
