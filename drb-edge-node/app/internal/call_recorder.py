import asyncio
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from app.config import settings
from app.internal import credentials
from app.internal.logger import logger

MAX_RECORDING_SECONDS = 600  # safety cap; drop call if it runs this long
PRE_BUFFER_SECONDS = 1.0  # seconds of audio to include before call_start
RING_BUFFER_SECONDS = 60  # how much history to keep when no call is active
READ_CHUNK_BYTES = 4096  # bytes per httpx read


class CallRecorder:
    """
    Maintains a persistent HTTP connection to the Icecast stream and buffers
    the raw MP3 bytes in a ring buffer.  When a call starts we note the
    monotonic clock; when it ends we slice the buffer and write the file.

    This approach eliminates per-call FFmpeg startup latency, which was
    causing empty recordings for calls shorter than ~1–2 s.
    """

    def __init__(self):
        self._recordings_dir = Path(settings.recordings_path)

        # Ring buffer: deque of (monotonic_time, bytes_chunk)
        self._buffer: deque[tuple[float, bytes]] = deque()
        self._buffer_bytes: int = 0

        self._stream_task: Optional[asyncio.Task] = None

        # Active call state
        self._call_id: Optional[str] = None
        self._call_start_mono: Optional[float] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the persistent stream buffer.  Call once from app lifespan."""
        self._stream_task = asyncio.create_task(self._stream_loop())
        logger.info("Stream ring-buffer started.")

    async def stop(self) -> None:
        """Cancel the stream reader."""
        if self._stream_task:
            self._stream_task.cancel()
            try:
                await self._stream_task
            except asyncio.CancelledError:
                pass
            self._stream_task = None

    # ------------------------------------------------------------------
    # Stream reader
    # ------------------------------------------------------------------

    async def _stream_loop(self) -> None:
        stream_url = (
            f"http://{settings.icecast_host}:{settings.icecast_port}"
            f"{settings.icecast_mount}"
        )
        timeout = httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)
        while True:
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    async with client.stream("GET", stream_url) as response:
                        response.raise_for_status()
                        logger.info(f"Stream buffer connected to {stream_url}")
                        async for chunk in response.aiter_bytes(READ_CHUNK_BYTES):
                            self._ingest(chunk)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Stream buffer disconnected ({e}) — retrying in 3 s")
                await asyncio.sleep(3)

    def _ingest(self, chunk: bytes) -> None:
        """Append a chunk and trim stale data from the front of the buffer."""
        now = time.monotonic()
        self._buffer.append((now, chunk))
        self._buffer_bytes += len(chunk)

        # During a call, never trim data newer than (call_start - pre_buffer).
        # Between calls, keep a rolling RING_BUFFER_SECONDS window.
        if self._call_start_mono is not None:
            keep_from = self._call_start_mono - PRE_BUFFER_SECONDS
        else:
            keep_from = now - RING_BUFFER_SECONDS

        while self._buffer:
            ts, old = self._buffer[0]
            if ts >= keep_from:
                break
            self._buffer.popleft()
            self._buffer_bytes -= len(old)

    # ------------------------------------------------------------------
    # Call recording API (same interface as before)
    # ------------------------------------------------------------------

    async def start_recording(self, call_id: str) -> bool:
        if self._call_id:
            logger.warning("Recording already active — ignoring start.")
            return False
        self._call_id = call_id
        self._call_start_mono = time.monotonic()
        logger.info(f"Recording started (ring-buffer): {call_id}")
        return True

    async def stop_recording(self) -> Optional[Path]:
        if not self._call_id:
            return None

        call_id = self._call_id
        call_start = self._call_start_mono
        self._call_id = None
        self._call_start_mono = None

        # Slice: everything from (call_start - pre_buffer) to now
        cutoff = (call_start - PRE_BUFFER_SECONDS) if call_start else 0.0
        chunks = [chunk for ts, chunk in self._buffer if ts >= cutoff]

        # Safety cap: if the call ran very long, truncate to MAX_RECORDING_SECONDS
        if call_start is not None:
            cap_cutoff = call_start + MAX_RECORDING_SECONDS
            now = time.monotonic()
            if now > cap_cutoff:
                # Approximate: trim chunks that arrived after the cap
                cap_keep_until = call_start + MAX_RECORDING_SECONDS
                chunks = [
                    chunk for ts, chunk in self._buffer
                    if cutoff <= ts <= cap_keep_until
                ]

        if not chunks:
            logger.warning(
                f"No buffered audio for call {call_id} — "
                "stream may not have been connected yet."
            )
            return None

        self._recordings_dir.mkdir(parents=True, exist_ok=True)
        ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        output_path = self._recordings_dir / f"{ts_str}_{call_id}.mp3"

        data = b"".join(chunks)
        output_path.write_bytes(data)

        size = output_path.stat().st_size
        if size > 0:
            logger.info(f"Recording saved: {output_path.name} ({size} bytes)")
            return output_path

        output_path.unlink(missing_ok=True)
        logger.warning(f"Recording for call {call_id} produced an empty file.")
        return None

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

    @property
    def is_recording(self) -> bool:
        return self._call_id is not None


call_recorder = CallRecorder()
