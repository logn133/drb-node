import asyncio
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
import httpx
from app.config import settings
from app.internal import credentials
from app.internal.logger import logger

MAX_RECORDING_SECONDS = 600  # 10 min safety cap; FFmpeg terminates long-running calls


class CallRecorder:
    def __init__(self):
        self._process: Optional[asyncio.subprocess.Process] = None
        self._current_call_id: Optional[str] = None
        self._current_file: Optional[Path] = None
        self._recordings_dir = Path(settings.recordings_path)

    async def start_recording(self, call_id: str) -> bool:
        if self._process:
            logger.warning("Recording already running — ignoring start.")
            return False

        self._recordings_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self._current_file = self._recordings_dir / f"{ts}_{call_id}.mp3"
        self._current_call_id = call_id

        # Read from the Icecast stream. burst-size=0 in icecast config ensures
        # each new connection starts at the live position with no buffered data.
        stream_url = f"http://{settings.icecast_host}:{settings.icecast_port}{settings.icecast_mount}"
        cmd = [
            "ffmpeg", "-y",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-i", stream_url,
            "-acodec", "libmp3lame",
            "-ar", "22050",
            "-b:a", "32k",
            "-t", str(MAX_RECORDING_SECONDS),
            str(self._current_file),
        ]

        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            
            # Quick check if it died immediately (e.g. Pulse source not found)
            await asyncio.sleep(0.5)
            if self._process.returncode is not None:
                _, stderr = await self._process.communicate()
                logger.error(f"FFmpeg exited immediately ({self._process.returncode}): {stderr.decode()}")
                return False
            
            logger.info(f"Recording process started: {self._current_file.name}")
            return True
        except Exception as e:
            logger.error(f"FFmpeg start failed: {e}")
            self._process = None
            self._current_file = None
            self._current_call_id = None
            return False

    async def stop_recording(self) -> Optional[Path]:
        if not self._process:
            return None

        proc = self._process
        output_file = self._current_file
        self._process = None
        self._current_file = None
        self._current_call_id = None

        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
        except ProcessLookupError:
            pass

        if output_file and output_file.exists() and output_file.stat().st_size > 0:
            logger.info(f"Recording saved: {output_file.name} ({output_file.stat().st_size} bytes)")
            return output_file

        logger.warning("Recording file empty or missing — discarding.")
        return None

    async def upload_recording(self, file_path: Path, call_id: str) -> Optional[str]:
        if not settings.c2_url:
            logger.info("No C2_URL configured — skipping upload.")
            return None

        upload_url = f"{settings.c2_url}/upload"
        api_key = credentials.get_api_key()
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

        try:
            async with httpx.AsyncClient(timeout=120) as client:
                with open(file_path, "rb") as f:
                    r = await client.post(
                        upload_url,
                        files={"file": (file_path.name, f, "audio/mpeg")},
                        data={"call_id": call_id, "node_id": settings.node_id},
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
        return self._process is not None


call_recorder = CallRecorder()
