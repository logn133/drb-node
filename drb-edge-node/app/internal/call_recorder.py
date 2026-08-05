"""
Continuous PulseAudio capture: a ring buffer for PRE-ROLL, a per-call
accumulator for the call itself.

A persistent capture process runs for the lifetime of the node. Spawning FFmpeg
per call used to lose the first 1-2 s to process startup, which meant short
transmissions produced empty files, so capture never stops.

TWO BUFFERS, TWO JOBS — this split is load-bearing:

  RING BUFFER   holds the last RING_BUFFER_SECONDS of audio at all times. Its
                only job is PRE-ROLL: however late we notice a grant, we can
                still seek back to OP25's exact timestamp. It is sized for
                detection latency, nothing else.

  ACCUMULATOR   opened by start_recording(), fed by every subsequent chunk, and
                closed by stop_recording(). Call length is therefore bounded by
                MAX_RECORDING_SECONDS alone — NOT by the ring buffer size. The
                old design sliced the finished call back out of the ring buffer,
                which silently clamped the front of any call longer than
                RING_BUFFER_SECONDS.

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
from app.internal import audio_trim, credentials, pulse
from app.internal.logger import logger

# Safety cap on a single recording; mirrors MAX_SEGMENT_SECONDS in metadata_watcher.
MAX_RECORDING_SECONDS = 600

# Audio included ahead of OP25's call_log timestamp. The grant is logged when the
# channel is granted, so the first syllable can land marginally before it.
#
# Kept small on purpose: measurement on a live node shows 1.71–2.45 s of real
# grant→speech delay on every call, so there is no clipping risk at the head and
# a larger pre-roll would only add dead air.
PRE_ROLL_SECONDS = 0.25

# Rolling history kept for PRE-ROLL ONLY. Budget for the worst realistic
# detection latency: 0.5 s poll interval + ~0.2 s http_server blocking floor +
# up to 3 s httpx timeout on a stalled poll + callback work ≈ 4 s from grant to
# start_recording(). 30 s is ~7x that margin, and at 16 kbps costs only ~60 KB of
# RAM. This value does NOT bound call length — the accumulator does.
RING_BUFFER_SECONDS = 30

# ~128 ms of audio per chunk at 16 kbps. Chunk size IS the timestamp resolution of
# the ring buffer, so it must stay well under PRE_ROLL_SECONDS — the old 4096-byte
# reads were ~2 s per chunk, which made sub-second slicing meaningless.
READ_CHUNK_BYTES = 256

# Encoder settings, matched on purpose to what Liquidsoap already pushes to
# Icecast — %mp3(bitrate=16, samplerate=22050, stereo=false) — so the C2 /upload
# endpoint keeps receiving exactly the kind of MP3 it has always received
# (multipart "audio/mpeg", stored to GCS as .mp3, then fed to Whisper).
# Change both of these together if you ever want higher-fidelity uploads.
MP3_BITRATE = "16k"
MP3_SAMPLE_RATE = "22050"

# Hard memory ceiling for one call's accumulator. 16 kbps is 2 KB/s, so 600 s of
# call is ~1.2 MB; 4x that is the ceiling, which both leaves room for encoder
# overshoot and guarantees a runaway call can never eat a Pi's RAM.
_MP3_BYTES_PER_SECOND = 16_000 // 8
MAX_RECORDING_BYTES = MAX_RECORDING_SECONDS * _MP3_BYTES_PER_SECOND * 4

# How long stop_recording() will wait for captured audio to actually reach the
# call's end timestamp. PulseAudio → FFmpeg → MP3 encoder → muxer → our pipe read
# is a pipeline with latency, so at the instant a call ends the newest buffered
# chunk is typically a few hundred ms OLDER than the end epoch. Slicing
# immediately therefore cuts the tail short — which costs the last word of the
# transmission, usually the disposition or the address. Bounded so a dead capture
# can never hang the upload path.
#
# Must exceed settings.call_tail_pad_seconds (default 3.0), otherwise a
# tgid_change close — which pads past a timestamp that is still ~now — gives up
# before the padded audio has been captured and warns on every talkgroup switch.
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


@dataclass
class _ActiveRecording:
    """Audio accumulating for the call currently being recorded."""

    call_id: str
    call_start: float           # OP25 grant epoch
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

    Bounds are accurate to ±one capture chunk (~128 ms).
    """

    call_id: str
    path: Optional[Path]
    audio_start_epoch: float
    audio_end_epoch: float
    lead_trimmed: float = 0.0
    tail_trimmed: float = 0.0
    clamped_seconds: float = 0.0
    all_silence: bool = False


class CallRecorder:
    """Continuous PulseAudio capture: ring buffer for pre-roll, accumulator per call."""

    def __init__(self):
        self._recordings_dir = Path(settings.recordings_path)

        # Ring buffer: deque of (wall_clock_epoch_at_arrival, mp3_bytes).
        # Pre-roll only — see the module docstring.
        self._buffer: deque[Tuple[float, bytes]] = deque()
        self._buffer_bytes: int = 0

        self._stream_task: Optional[asyncio.Task] = None
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._capturing: bool = False

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
            "-ac", "1",
            "-ar", MP3_SAMPLE_RATE,
            "-b:a", MP3_BITRATE,
            # Without this, the MP3 muxer fills its 32 KB AVIO buffer before
            # writing anything — 16 s of audio per burst at 16 kbps, which would
            # destroy the arrival timestamps the slicing depends on.
            "-flush_packets", "1",
            "-f", "mp3", "-",
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
        logger.info(f"Starting capture: ffmpeg -f pulse -i {settings.pulse_source}")
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
                chunk = await proc.stdout.read(READ_CHUNK_BYTES)
                if not chunk:
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

    def _ingest(self, chunk: bytes) -> None:
        """Append a chunk to the ring buffer and, if recording, the accumulator."""
        now = time.time()
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
        Open a recording. `start_epoch` is OP25's call_log timestamp (host wall
        clock); the slice begins PRE_ROLL_SECONDS before it. Omit it only when no
        OP25 timestamp is available — then we fall back to "now", losing precision.
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
            # or OP25's timestamp is far in the past. Clamp and say so LOUDLY —
            # this is silent audio loss otherwise.
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

    async def stop_recording(self, end_epoch: Optional[float] = None) -> Optional[Recording]:
        """
        Close the recording and write the file. `end_epoch` is host wall clock.

        Waits (bounded) for captured audio to actually cover `end_epoch` before
        slicing — see TAIL_WAIT_TIMEOUT_SECONDS. Returns None only when there was
        no recording open or no audio at all.
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

        chunks: List[bytes] = []
        last_ts = slice_start
        for ts, chunk in active.chunks:
            if ts < slice_start:
                continue
            chunks.append(chunk)
            last_ts = ts
            if ts >= end:
                # Include the chunk straddling `end` so the tail is never clipped,
                # then stop.
                break

        if not chunks:
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

        self._recordings_dir.mkdir(parents=True, exist_ok=True)
        ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        output_path = self._recordings_dir / f"{ts_str}_{call_id}.mp3"

        output_path.write_bytes(b"".join(chunks))

        size = output_path.stat().st_size
        if size <= 0:
            output_path.unlink(missing_ok=True)
            logger.warning(f"Recording for call {call_id} produced an empty file.")
            return None

        audio_end = min(end, last_ts)
        logger.info(f"Recording saved: {output_path.name} ({size} bytes, {audio_end - slice_start:.2f}s window)")

        recording = Recording(
            call_id=call_id,
            path=output_path,
            audio_start_epoch=slice_start,
            audio_end_epoch=audio_end,
            clamped_seconds=active.clamped_seconds,
        )
        return await self._apply_trim(recording)

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

    async def _apply_trim(self, recording: Recording) -> Recording:
        """
        Strip leading/trailing dead air and keep the timing metadata honest.

        An all-silence recording is NOT uploaded: it carries no information and
        silence is exactly what makes Whisper hallucinate. It is logged instead,
        because it also means something is wrong with the audio path.
        """
        if not settings.trim_silence or recording.path is None:
            return recording

        result = await audio_trim.trim_silence(
            recording.path,
            sample_rate=MP3_SAMPLE_RATE,
            bitrate=MP3_BITRATE,
        )

        if result.all_silence:
            logger.warning(
                f"Call {recording.call_id} contains no speech at all — skipping upload. "
                "Check squelch, the Liquidsoap output and the drb_sink monitor."
            )
            recording.path.unlink(missing_ok=True)
            recording.path = None
            recording.all_silence = True
            return recording

        if result.applied:
            recording.lead_trimmed = result.lead
            recording.tail_trimmed = result.tail
            # Wall clock of the trimmed audio's first and last sample.
            recording.audio_start_epoch += result.lead
            recording.audio_end_epoch -= result.tail
        return recording

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
