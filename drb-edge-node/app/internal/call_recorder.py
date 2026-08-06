"""
Continuous PulseAudio capture: a ring buffer for PRE-ROLL, a per-call
accumulator for the call itself, and the voice-activity signal that decides
where calls begin and end.

A persistent capture process runs for the lifetime of the node. Spawning FFmpeg
per call used to lose the first 1-2 s to process startup, which meant short
transmissions produced empty files, so capture never stops.

RAW PCM, NOT MP3 — this is the change everything else hangs off. FFmpeg is
asked for s16le/22050/mono on stdout instead of an MP3 stream, so:

  * silence detection is integer arithmetic over each chunk as it arrives, with
    no decode, which is what makes AUDIO-DRIVEN call boundaries possible;
  * trimming is a byte-offset slice, not a second FFmpeg pass;
  * MP3 encoding happens exactly ONCE, at save time, so uploads are no longer
    double-encoded.

TWO BUFFERS, TWO JOBS — this split is load-bearing:

  RING BUFFER   holds the last RING_BUFFER_SECONDS of audio at all times. Its
                only job is PRE-ROLL: however late the segmenter notices voice
                onset, we can still seek back before it. It is sized for
                detection latency, nothing else.

  ACCUMULATOR   opened by start_recording(), fed by every subsequent chunk, and
                closed by stop_recording(). Call length is therefore bounded by
                MAX_RECORDING_SECONDS alone — NOT by the ring buffer size. The
                old design sliced the finished call back out of the ring buffer,
                which silently clamped the front of any call longer than
                RING_BUFFER_SECONDS.

VOICE ACTIVITY is tracked CONTINUOUSLY, not only while recording, because the
segmenter starts a call from audio onset. `_last_voice_epoch` and
`_voice_onset_epoch` are the whole interface: metadata_watcher polls them via
audio_activity() and owns every decision about segment boundaries. This module
deliberately does not open or close calls by itself — attribution (which
talkgroup this audio belongs to) lives in the watcher, and audio alone cannot
answer it.

Why not Icecast: it lags ~1 s at connect and drifts progressively to 100 s+, so
slice timestamps and audio content diverge without bound. Icecast stays in the
stack for frontend/mobile live listening; it is not an accuracy path.

CLOCK DOMAIN: chunks are stamped with time.time(), the host wall clock. OP25's
call_log timestamps are time.time() from inside the op25 container. All client
containers run network_mode: host and share the host kernel clock, so the two are
the same clock and the pre-roll arithmetic below is a direct subtraction with no
offset mapping. (A wall-clock STEP — e.g. a large NTP correction — would corrupt
at most the calls in flight at that instant; the buffer self-heals within
RING_BUFFER_SECONDS.)

NOTE on what a chunk timestamp means: it is the ARRIVAL time of that audio at
this process, which lags the moment the words were spoken by the PulseAudio →
FFmpeg → pipe latency. Every timestamp this module produces is in that same
arrival clock, so differences between them are exact; only comparisons against
OP25's control-channel timestamps carry the lag, and those are padded for it.
"""
import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

import httpx
from app.config import settings
from app.internal import audio_trim, credentials, pcm, pulse
from app.internal.logger import logger

# Safety cap on a single recording; mirrors MAX_SEGMENT_SECONDS in metadata_watcher.
MAX_RECORDING_SECONDS = 600

# Audio included ahead of the detected voice onset.
#
# Under audio-driven segmentation this is no longer covering a variable
# control-channel offset — it covers exactly two things: the analysis chunk
# quantisation (~46 ms) and the possibility that the first syllable ramps up
# through the silence threshold rather than crossing it instantly. 0.25 s is
# generous for both, and anything it drags in that is genuinely silence gets
# trimmed off again before upload.
PRE_ROLL_SECONDS = 0.25

# Rolling history kept for PRE-ROLL ONLY. Budget for the worst realistic
# detection latency: the segmenter polls every 0.5 s and can stall for up to a
# 3 s httpx timeout on a bad OP25 poll, so ~4 s from onset to start_recording().
# 30 s is ~7x that margin. At 44.1 KB/s of PCM it costs ~1.3 MB of RAM. This
# value does NOT bound call length — the accumulator does.
RING_BUFFER_SECONDS = 30

# ~46 ms of audio per chunk. Chunk size is BOTH the timestamp resolution of the
# ring buffer AND the window silence detection runs over, so it has to stay well
# under PRE_ROLL_SECONDS and well under the shortest utterance we care about.
READ_CHUNK_BYTES = 2048

# Encoder settings for the single encode at save time, matched on purpose to
# what Liquidsoap already pushes to Icecast — %mp3(bitrate=16, samplerate=22050,
# stereo=false) — so the C2 /upload endpoint keeps receiving exactly the kind of
# MP3 it has always received (multipart "audio/mpeg", stored to GCS as .mp3,
# then fed to Whisper). MP3_SAMPLE_RATE MUST equal pcm.SAMPLE_RATE: the encode
# is a straight pass with no resampling.
MP3_BITRATE = "16k"
MP3_SAMPLE_RATE = str(pcm.SAMPLE_RATE)

# Bounded so a wedged encoder can never stall the upload path.
ENCODE_TIMEOUT_SECONDS = 60.0

# Hard memory ceiling for one call's accumulator.
#
# PCM costs 44.1 KB/s where the old MP3 buffer cost 2 KB/s, so this had to be
# re-derived rather than carried over. 600 s (the time cap) of PCM is 26.5 MB;
# 32 MiB is ~761 s, which guarantees the TIME cap always bites first and a legal
# call is never truncated by the byte cap. Peak resident audio is therefore
# ~33.5 MB for the accumulator plus ~1.3 MB for the ring buffer.
#
# Rejected alternatives, for the record: spilling to disk (SD-card wear on a Pi,
# and I/O in the close path); encoding incrementally into MP3 as chunks arrive
# (puts a subprocess back in the hot path and makes the sample-accurate post-hoc
# trim impossible); a lower capture sample rate (changes what Whisper receives).
MAX_RECORDING_BYTES = 32 * 1024 * 1024

# How long stop_recording() will wait for captured audio to actually reach the
# call's end timestamp. PulseAudio → FFmpeg → our pipe read is a pipeline with
# latency, so at the instant a CONTROL-CHANNEL derived end is computed the
# newest buffered chunk is typically a few hundred ms OLDER than that epoch.
# Slicing immediately therefore cuts the tail short — which costs the last word
# of the transmission, usually the disposition or the address.
#
# An audio-driven close never needs this (its end epoch is derived from audio
# that is already buffered, by construction), but a tgid_change close still
# pads past a control-channel timestamp that is ~now, so the wait must exceed
# settings.call_tail_pad_seconds (default 3.0) or it would warn on every
# talkgroup switch. Bounded so a dead capture can never hang the upload path.
TAIL_WAIT_TIMEOUT_SECONDS = 4.0
TAIL_WAIT_POLL_SECONDS = 0.05

# Backoff bounds for restarting a dead capture process.
RESTART_BACKOFF_MIN = 1.0
RESTART_BACKOFF_MAX = 15.0

# Substrings looked for in FFmpeg's stderr to classify why capture exited.
# "No such process" is what FFmpeg's pulse input prints when the daemon is up
# but the named source does not exist — a real PULSE_SOURCE misconfiguration,
# not a startup race. This is exactly the failure mode that hid unnoticed
# behind a generic "restarting" log in the April outage: silently retrying
# forever against a wrong source name looks identical to a normal startup
# wait unless it is logged differently.
_SOURCE_MISSING_MARKERS = ("no such process", "no such device")
# "Connection refused"/"Connection failure" is what the pulse client library
# prints when nothing is listening on the socket at all — expected while the
# op25 container's daemon is still coming up.
_NO_DAEMON_MARKERS = ("connection refused", "connection failure")


@dataclass(frozen=True)
class AudioActivity:
    """
    What the capture stream is doing right now, as raw facts.

    Deliberately carries no decision — no "should a call be open" boolean —
    because every threshold comparison belongs to metadata_watcher, which owns
    the segment state machine and (in tests) an injectable clock. This is a
    snapshot of observations, nothing more.

    `voice_onset_epoch` is the arrival timestamp of the first chunk of the most
    recent run of voice. It is NOT cleared when that run ends, so a caller must
    check `last_voice_epoch` against its own clock before treating the run as
    live. That is intentional: the segmenter needs the onset of the run it just
    finished recording in order to avoid re-opening on the same run.
    """

    capturing: bool
    recording: bool
    last_voice_epoch: Optional[float] = None
    voice_onset_epoch: Optional[float] = None
    # Convenience for /api/status only; the segmenter recomputes this against
    # its own clock.
    silence_seconds: float = 0.0


@dataclass
class _ActiveRecording:
    """Audio accumulating for the call currently being recorded."""

    call_id: str
    call_start: float           # detected voice onset (or a caller-supplied epoch)
    slice_start: float          # call_start - PRE_ROLL_SECONDS
    chunks: List[Tuple[float, bytes]] = field(default_factory=list)
    total_bytes: int = 0
    # Seconds of requested pre-roll that were not in the buffer at open time.
    clamped_seconds: float = 0.0
    # True once the byte ceiling was hit and audio started being dropped.
    truncated_by_cap: bool = False


@dataclass
class Recording:
    """
    A finished recording plus the timing metadata needed to map audio position
    back to wall clock.

    `started_at`/`ended_at` upstream keep meaning the CALL's bounds. These are
    the AUDIO's bounds, which differ once silence is trimmed:

        wall_clock_of(audio_offset_t) == audio_start_epoch + t

    Bounds are accurate to ±one capture chunk (~46 ms).
    """

    call_id: str
    path: Optional[Path]
    audio_start_epoch: float
    audio_end_epoch: float
    lead_trimmed: float = 0.0
    tail_trimmed: float = 0.0
    clamped_seconds: float = 0.0
    all_silence: bool = False


async def encode_mp3(audio: bytes, path: Path) -> bool:
    """
    The one and only encode in the pipeline: raw PCM in, MP3 file out.

    Module-level rather than a method so tests can substitute it without
    needing FFmpeg, and so the "exactly one encode per call" property is
    trivially observable.
    """
    if not audio:
        return False
    cmd = [
        "ffmpeg",
        "-hide_banner", "-nostdin", "-nostats",
        "-loglevel", "warning", "-y",
        "-f", "s16le",
        "-ar", MP3_SAMPLE_RATE,
        "-ac", str(pcm.CHANNELS),
        "-i", "pipe:0",
        "-ar", MP3_SAMPLE_RATE,
        "-ac", str(pcm.CHANNELS),
        "-b:a", MP3_BITRATE,
        "-f", "mp3", str(path),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:
        logger.error(f"Could not launch the MP3 encoder ({e}) — recording not saved.")
        return False

    try:
        _, stderr = await asyncio.wait_for(proc.communicate(audio), timeout=ENCODE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        logger.error(f"MP3 encode timed out after {ENCODE_TIMEOUT_SECONDS:.0f}s — recording not saved.")
        return False
    except Exception as e:
        logger.error(f"MP3 encode failed ({e}) — recording not saved.")
        return False

    if proc.returncode != 0:
        logger.error(f"MP3 encode exited {proc.returncode}: {stderr.decode(errors='replace').strip()}")
        return False
    return True


class CallRecorder:
    """Continuous PulseAudio capture: ring buffer for pre-roll, accumulator per call."""

    def __init__(self):
        self._recordings_dir = Path(settings.recordings_path)

        # Ring buffer: deque of (wall_clock_epoch_at_arrival, pcm_bytes).
        # Pre-roll only — see the module docstring.
        self._buffer: deque[Tuple[float, bytes]] = deque()
        self._buffer_bytes: int = 0

        self._stream_task: Optional[asyncio.Task] = None
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._capturing: bool = False

        # Voice activity, tracked continuously — not only while recording.
        self._last_voice_epoch: Optional[float] = None
        self._voice_onset_epoch: Optional[float] = None

        # Active recording state (None when idle)
        self._active: Optional[_ActiveRecording] = None

        # Last few lines of the most recent FFmpeg stderr, used to classify
        # why a capture process exited (see _classify_capture_exit).
        self._last_stderr_lines: deque[str] = deque(maxlen=10)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the persistent capture. Call once from app lifespan."""
        self._stream_task = asyncio.create_task(self._capture_loop())
        logger.info("PulseAudio ring-buffer starting.")

    async def stop(self) -> None:
        if self._stream_task:
            self._stream_task.cancel()
            try:
                await self._stream_task
            except asyncio.CancelledError:
                pass
            self._stream_task = None
        await self._terminate_proc()

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------

    def _ffmpeg_command(self) -> List[str]:
        return [
            "ffmpeg",
            "-hide_banner", "-nostdin", "-nostats",
            "-loglevel", "warning",
            "-f", "pulse", "-i", settings.pulse_source,
            "-ac", str(pcm.CHANNELS),
            "-ar", MP3_SAMPLE_RATE,
            # Raw PCM on stdout. No muxer, so no -flush_packets games: s16le is
            # a bare byte stream and every byte FFmpeg produces is immediately
            # readable, which is what keeps arrival timestamps honest.
            "-f", "s16le", "-",
        ]

    async def _capture_loop(self) -> None:
        backoff = RESTART_BACKOFF_MIN
        while True:
            try:
                # The original bug this path was abandoned for: FFmpeg was launched
                # with -f pulse before the shared socket existed, failed instantly,
                # and never recovered. Wait for it, bounded, every time.
                if not await pulse.wait_until_ready():
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, RESTART_BACKOFF_MAX)
                    continue

                await self._run_capture()
                self._log_capture_exit()
            except asyncio.CancelledError:
                await self._terminate_proc()
                raise
            except Exception as e:
                logger.warning(f"PulseAudio capture error ({e}) — restarting.")

            self._capturing = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RESTART_BACKOFF_MAX)

    async def _run_capture(self) -> None:
        cmd = self._ffmpeg_command()
        logger.info(f"Starting capture: ffmpeg -f pulse -i {settings.pulse_source} (s16le/{MP3_SAMPLE_RATE}/mono)")
        self._last_stderr_lines.clear()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._proc = proc
        stderr_task = asyncio.create_task(self._drain_stderr(proc))
        try:
            assert proc.stdout is not None
            while True:
                # readexactly, not read: a fixed chunk keeps every analysis
                # window the same length AND guarantees sample alignment, so a
                # short read can never split a 16-bit sample across chunks.
                try:
                    chunk = await proc.stdout.readexactly(READ_CHUNK_BYTES)
                except asyncio.IncompleteReadError as partial:
                    if partial.partial:
                        self._ingest(partial.partial)
                    break  # EOF — FFmpeg died or the source went away
                if not self._capturing:
                    self._capturing = True
                    logger.info("PulseAudio capture is producing audio.")
                self._ingest(chunk)
        finally:
            self._capturing = False
            # _drain_stderr swallows its own CancelledError, so it finishes cleanly
            # and never needs awaiting here.
            stderr_task.cancel()
            await self._terminate_proc()

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        """Surface FFmpeg's diagnostics instead of letting the pipe fill and block."""
        if proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                text = line.decode(errors="replace").strip()
                self._last_stderr_lines.append(text)
                logger.warning(f"ffmpeg(pulse): {text}")
        except asyncio.CancelledError:
            return
        except Exception:
            return

    def _log_capture_exit(self) -> None:
        """
        Log why the just-finished capture process exited, distinguishing the
        two failure modes that matter operationally instead of one generic
        "restarting" line for both:

          - no daemon / connection refused: infrastructure isn't up yet. This
            is expected during startup/op25 restarts, so it stays at INFO —
            the retry loop above already handles it.
          - daemon up but the named source is missing: almost always a real
            PULSE_SOURCE misconfiguration. This gets a loud, distinct ERROR
            naming the configured source, because silently retrying forever
            against a wrong source name is exactly how this hid in the past.
        """
        text = " ".join(self._last_stderr_lines).lower()

        if any(marker in text for marker in _SOURCE_MISSING_MARKERS):
            logger.error(
                f"PulseAudio capture exited: source '{settings.pulse_source}' does not exist on the "
                "daemon (FFmpeg reported 'No such process'). This looks like a real PULSE_SOURCE "
                "misconfiguration or a missing drb_sink — retrying will not fix it by itself. "
                "Restarting anyway."
            )
            return

        if any(marker in text for marker in _NO_DAEMON_MARKERS):
            logger.info(
                "PulseAudio capture exited: daemon not accepting connections — "
                "infrastructure still coming up, restarting."
            )
            return

        logger.warning("PulseAudio capture process exited — restarting.")

    async def _terminate_proc(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.returncode is not None:
            return
        try:
            # Synchronous, so the signal lands even if we are being cancelled and
            # the reap below never gets to run.
            proc.terminate()
        except Exception:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Voice activity
    # ------------------------------------------------------------------

    def _note_voice(self, chunk: bytes, now: float) -> None:
        """
        Update the continuous voice-activity marks from one arriving chunk.

        A chunk carrying signal starts a NEW run whenever the gap since the last
        one is at least the configured silence timeout — i.e. runs are separated
        by exactly the same threshold that ends a recording, so the segmenter's
        "did a new run begin" and "did the recording end" questions can never
        disagree with each other.
        """
        if pcm.is_silent(chunk, settings.call_silence_threshold_db):
            return
        gap = settings.call_silence_timeout
        if self._last_voice_epoch is None or (now - self._last_voice_epoch) >= gap:
            self._voice_onset_epoch = now
        self._last_voice_epoch = now

    def audio_activity(self) -> AudioActivity:
        """Snapshot of the capture stream for metadata_watcher (and /api/status)."""
        last = self._last_voice_epoch
        silence = (time.time() - last) if last is not None else 0.0
        return AudioActivity(
            capturing=self._capturing,
            recording=self._active is not None,
            last_voice_epoch=last,
            voice_onset_epoch=self._voice_onset_epoch,
            silence_seconds=max(0.0, silence),
        )

    def _ingest(self, chunk: bytes) -> None:
        """Append a chunk to the ring buffer and, if recording, the accumulator."""
        now = time.time()
        self._note_voice(chunk, now)

        self._buffer.append((now, chunk))
        self._buffer_bytes += len(chunk)

        # The ring buffer serves pre-roll only, so it is trimmed to a fixed
        # window unconditionally — an open recording no longer pins it, because
        # the accumulator owns that audio.
        keep_from = now - RING_BUFFER_SECONDS
        while self._buffer and self._buffer[0][0] < keep_from:
            _, old = self._buffer.popleft()
            self._buffer_bytes -= len(old)

        active = self._active
        if active is None:
            return

        if active.total_bytes + len(chunk) > MAX_RECORDING_BYTES:
            if not active.truncated_by_cap:
                active.truncated_by_cap = True
                logger.warning(
                    f"Recording {active.call_id} hit the {MAX_RECORDING_BYTES} byte memory ceiling "
                    f"after {now - active.slice_start:.1f}s — further audio is being dropped."
                )
            return

        active.chunks.append((now, chunk))
        active.total_bytes += len(chunk)

    # ------------------------------------------------------------------
    # Recording API
    # ------------------------------------------------------------------

    async def start_recording(self, call_id: str, start_epoch: Optional[float] = None) -> bool:
        """
        Open a recording. `start_epoch` is the detected voice onset (host wall
        clock, same clock as the chunk stamps); the slice begins
        PRE_ROLL_SECONDS before it. Omit it only when no onset is available —
        then we fall back to "now", losing the pre-roll's precision.
        """
        if self._active is not None:
            logger.warning(f"Recording already active ({self._active.call_id}) — ignoring start for {call_id}.")
            return False

        call_start = start_epoch if start_epoch else time.time()
        slice_start = call_start - PRE_ROLL_SECONDS

        if not self._capturing:
            logger.warning(f"Recording {call_id} opened while PulseAudio capture is down — audio may be missing.")

        # Seed the accumulator with the pre-roll already sitting in the ring
        # buffer. No await between reading the buffer and publishing _active, so
        # the capture task cannot slip a chunk in between and double-count it.
        clamped = 0.0
        oldest = self._buffer[0][0] if self._buffer else None
        if oldest is not None and slice_start < oldest:
            # Pre-roll predates the buffer: node just started, capture restarted,
            # or the onset is far in the past. Clamp and say so LOUDLY — this is
            # silent audio loss otherwise.
            clamped = oldest - slice_start
            logger.warning(
                f"BUFFER CLAMP: pre-roll for call {call_id} predates buffered audio by "
                f"{clamped:.2f}s — recording starts at the buffer head and that audio is lost."
            )

        seeded = [(ts, chunk) for ts, chunk in self._buffer if ts >= slice_start]
        self._active = _ActiveRecording(
            call_id=call_id,
            call_start=call_start,
            slice_start=slice_start,
            chunks=seeded,
            total_bytes=sum(len(c) for _, c in seeded),
            clamped_seconds=clamped,
        )

        logger.info(f"Recording started: {call_id} (slice from {slice_start:.3f})")
        return True

    async def discard_recording(self) -> None:
        """
        Drop the open recording without writing anything.

        Used when the segmenter decides the audio must not be kept — today only
        the unattributed/orphan-audio path, where uploading would inject a call
        with no talkgroup into correlation.
        """
        active, self._active = self._active, None
        if active is not None:
            logger.info(f"Discarded buffered audio for {active.call_id} ({active.total_bytes} bytes).")

    async def stop_recording(self, end_epoch: Optional[float] = None) -> Optional[Recording]:
        """
        Close the recording, trim it, encode it once and write the file.
        `end_epoch` is host wall clock.

        Waits (bounded) for captured audio to actually cover `end_epoch` before
        slicing — see TAIL_WAIT_TIMEOUT_SECONDS. An audio-driven close never
        needs that wait, because its end epoch is derived from audio that is
        already buffered; a control-channel-derived close (tgid_change) does.
        Returns None only when there was no recording open or no audio at all.
        """
        active = self._active
        if active is None:
            return None

        call_id = active.call_id
        slice_start = active.slice_start

        end = end_epoch if end_epoch else time.time()
        end = min(end, active.call_start + MAX_RECORDING_SECONDS)

        # The accumulator keeps filling during this wait — that is the point.
        await self._await_tail(end, call_id)
        self._active = None

        parts: List[bytes] = []
        total = 0
        last_ts = slice_start
        for ts, chunk in active.chunks:
            if ts < slice_start:
                continue
            parts.append(chunk)
            total += len(chunk)
            last_ts = ts
            if ts >= end:
                # Include the chunk straddling `end` so the tail is never clipped,
                # then stop.
                break

        if not parts:
            logger.warning(
                f"No buffered audio for call {call_id} "
                f"(window {slice_start:.3f}–{end:.3f}) — PulseAudio capture may be down."
            )
            return None

        if last_ts < end - TAIL_WAIT_POLL_SECONDS:
            logger.warning(
                f"BUFFER CLAMP: call {call_id} ends {end - last_ts:.2f}s after the newest captured "
                "audio — the tail is short. Capture may be stalled or restarting."
            )

        raw = b"".join(parts)
        recording = Recording(
            call_id=call_id,
            path=None,
            audio_start_epoch=slice_start,
            audio_end_epoch=slice_start + pcm.seconds(len(raw)),
            clamped_seconds=active.clamped_seconds,
        )
        return await self._finish(recording, raw)

    async def _finish(self, recording: Recording, raw: bytes) -> Optional[Recording]:
        """Trim, encode exactly once, and write the MP3."""
        audio = raw
        if settings.trim_silence:
            audio, result = audio_trim.trim_pcm(audio)
            if result.all_silence:
                logger.warning(
                    f"Call {recording.call_id} contains no speech at all — skipping upload. "
                    "Check squelch, the Liquidsoap output and the drb_sink monitor."
                )
                recording.all_silence = True
                return recording
            if result.applied:
                recording.lead_trimmed = result.lead
                recording.tail_trimmed = result.tail
                # Wall clock of the trimmed audio's first and last sample.
                recording.audio_start_epoch += result.lead
                recording.audio_end_epoch -= result.tail

        self._recordings_dir.mkdir(parents=True, exist_ok=True)
        ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        output_path = self._recordings_dir / f"{ts_str}_{recording.call_id}.mp3"

        if not await encode_mp3(audio, output_path):
            output_path.unlink(missing_ok=True)
            return None

        size = output_path.stat().st_size if output_path.exists() else 0
        if size <= 0:
            output_path.unlink(missing_ok=True)
            logger.warning(f"Recording for call {recording.call_id} produced an empty file.")
            return None

        recording.path = output_path
        logger.info(
            f"Recording saved: {output_path.name} ({size} bytes, "
            f"{pcm.seconds(len(audio)):.2f}s audio)"
        )
        return recording

    async def _await_tail(self, end: float, call_id: str) -> float:
        """
        Block until captured audio reaches `end`, or the bounded timeout expires.

        Returns seconds waited. Logs whenever a wait was actually needed so the
        real pipeline latency is observable in the field.
        """
        if not self._buffer:
            return 0.0
        if self._buffer[-1][0] >= end:
            return 0.0

        started = time.monotonic()
        deadline = started + TAIL_WAIT_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            await asyncio.sleep(TAIL_WAIT_POLL_SECONDS)
            if self._buffer and self._buffer[-1][0] >= end:
                waited = time.monotonic() - started
                logger.info(f"Waited {waited:.2f}s for the tail of call {call_id} to reach the buffer.")
                return waited
            if not self._capturing:
                break  # capture died mid-wait; nothing more is coming

        waited = time.monotonic() - started
        newest = self._buffer[-1][0] if self._buffer else end
        logger.warning(
            f"Tail wait for call {call_id} gave up after {waited:.2f}s — captured audio is still "
            f"{max(0.0, end - newest):.2f}s short of the call end. Tail may be clipped."
        )
        return waited

    # ------------------------------------------------------------------
    # Upload (unchanged interface)
    # ------------------------------------------------------------------

    async def upload_recording(
        self,
        file_path: Path,
        call_id: str,
        talkgroup_id: Optional[int] = None,
        talkgroup_name: Optional[str] = None,
        system_id: Optional[str] = None,
        audio_start_epoch: Optional[float] = None,
        audio_end_epoch: Optional[float] = None,
    ) -> Optional[str]:
        if not settings.c2_url:
            logger.info("No C2_URL configured — skipping upload.")
            return None

        upload_url = f"{settings.c2_url}/upload"
        api_key = credentials.get_api_key()
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

        form: dict = {"call_id": call_id, "node_id": settings.node_id}
        if talkgroup_id is not None:
            form["talkgroup_id"] = str(talkgroup_id)
        if talkgroup_name:
            form["talkgroup_name"] = talkgroup_name
        if system_id:
            form["system_id"] = system_id
        # Where this audio really sits on the wall clock once silence is trimmed.
        # C2 does not declare these Form fields yet, so FastAPI ignores them —
        # they cost nothing and are here for when playback/correlation want them.
        if audio_start_epoch is not None:
            form["audio_start_epoch"] = f"{audio_start_epoch:.3f}"
        if audio_end_epoch is not None:
            form["audio_end_epoch"] = f"{audio_end_epoch:.3f}"

        try:
            async with httpx.AsyncClient(timeout=120) as client:
                with open(file_path, "rb") as f:
                    r = await client.post(
                        upload_url,
                        files={"file": (file_path.name, f, "audio/mpeg")},
                        data=form,
                        headers=headers,
                    )
                    r.raise_for_status()
                    audio_url = r.json().get("url")
                    logger.info(f"Upload complete: {audio_url}")
                    return audio_url
        except Exception as e:
            logger.error(f"Upload failed: {e}")
            return None
        finally:
            try:
                file_path.unlink()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def is_recording(self) -> bool:
        return self._active is not None

    @property
    def is_capturing(self) -> bool:
        """True when FFmpeg is alive and audio is actually arriving."""
        return self._capturing

    @property
    def buffered_seconds(self) -> float:
        if len(self._buffer) < 2:
            return 0.0
        return self._buffer[-1][0] - self._buffer[0][0]


call_recorder = CallRecorder()
