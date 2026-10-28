"""
On-disk spool for recordings whose upload to C2 failed (node-26#44).

Before this, `upload_recording` deleted the file in a `finally` whether or not
the POST succeeded, so a C2 outage, a network blip, a 5xx or a timeout lost the
call's audio for good — and with it the transcript and the incident link.

Now a recording is deleted only after a 2xx that carries a storage URL. A
failure that can succeed later is MOVED into this spool with a JSON sidecar
holding everything needed to re-POST it, and a background task retries with
exponential backoff. The spool is drained on startup, so a reboot or container
restart does not lose it either.

Failure classes (see `classify`):

  retry    network error, timeout, 5xx, 2xx with no URL (C2 accepted the
           request but storage failed), 401 (a reissued key is on its way
           over MQTT/key pickup), 403 (not approved yet), 408, 429
  reject   any other 4xx — 400 empty file, 413 too large.
           Retrying the same bytes cannot change the answer, so the file is
           deleted with an ERROR log naming the status.

BOUNDED on purpose: /recordings is a bind mount of the host disk, and a full
disk already stopped node-002 recording for 17 days (node-26#41). Entries older
than `upload_spool_max_age_hours` are dropped, and past
`upload_spool_max_bytes` the OLDEST entries go first — the newest audio is the
most likely to still matter to a live incident.

File layout, one pair per call:

    <spool>/<recording name>.flac
    <spool>/<recording name>.flac.json     sidecar (written FIRST, atomically)

The sidecar is written before the audio is moved in, and deleted after the
audio is removed, so a crash at any point leaves at worst a sidecar with no
audio — which the next scan cleans up — and never audio that cannot be
re-uploaded.
"""
import asyncio
import json
import os
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import httpx

from app.config import settings
from app.internal import credentials
from app.internal.logger import logger

SIDECAR_SUFFIX = ".json"
UPLOAD_TIMEOUT_SECONDS = 120.0
# Statuses in the 4xx range that are worth retrying unchanged.
# 403 = node not approved yet: approval can arrive later, and the spool caps
# bound what a never-approved node keeps.
RETRYABLE_4XX = {401, 403, 408, 429}

OK, RETRY, REJECT = "ok", "retry", "reject"


@dataclass
class UploadMeta:
    """Everything /upload needs besides the file itself."""

    call_id: str
    talkgroup_id: Optional[int] = None
    talkgroup_name: Optional[str] = None
    system_id: Optional[str] = None
    audio_start_epoch: Optional[float] = None
    audio_end_epoch: Optional[float] = None
    # Further form fields, sent as-is (Trunk Recorder calls carry units,
    # emergency, encryption — node-26#45). Kept in the sidecar like the rest.
    extra: Optional[Dict[str, str]] = None


# By extension, so a spooled file is re-sent with the type it was first sent
# with — the PulseAudio recorder writes FLAC, Trunk Recorder WAV.
AUDIO_MIME_BY_SUFFIX = {".flac": "audio/flac", ".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4"}


def mime_for(path: Path, default: str = "application/octet-stream") -> str:
    return AUDIO_MIME_BY_SUFFIX.get(path.suffix.lower(), default)


@dataclass
class UploadResult:
    kind: str                     # OK | RETRY | REJECT
    url: Optional[str] = None
    status: Optional[int] = None  # HTTP status, None for a network error
    detail: str = ""


def classify(status: int, url: Optional[str]) -> str:
    if 200 <= status < 300:
        # C2 answers 200 {"url": null} when its GCS write failed: nothing was
        # stored, so this is a server-side failure, not a success.
        return OK if url else RETRY
    if status in RETRYABLE_4XX or status >= 500:
        return RETRY
    if 400 <= status < 500:
        return REJECT
    return RETRY


def _form(meta: UploadMeta) -> Dict[str, str]:
    form: Dict[str, str] = {"call_id": meta.call_id, "node_id": settings.node_id}
    if meta.talkgroup_id is not None:
        form["talkgroup_id"] = str(meta.talkgroup_id)
    if meta.talkgroup_name:
        form["talkgroup_name"] = meta.talkgroup_name
    if meta.system_id:
        form["system_id"] = meta.system_id
    # Where this audio really sits on the wall clock once silence is trimmed.
    # C2 does not declare these Form fields yet, so FastAPI ignores them —
    # they cost nothing and are here for when playback/correlation want them.
    if meta.audio_start_epoch is not None:
        form["audio_start_epoch"] = f"{meta.audio_start_epoch:.3f}"
    if meta.audio_end_epoch is not None:
        form["audio_end_epoch"] = f"{meta.audio_end_epoch:.3f}"
    for key, value in (meta.extra or {}).items():
        if value is not None and key not in form:
            form[key] = str(value)
    return form


async def post_recording(
    file_path: Path, meta: UploadMeta, mime: str, timeout: float = UPLOAD_TIMEOUT_SECONDS,
) -> UploadResult:
    """One POST to C2 /upload. Never raises; never touches the file on disk."""
    upload_url = f"{settings.c2_url}/upload"
    api_key = credentials.get_api_key()
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            with open(file_path, "rb") as f:
                r = await client.post(
                    upload_url,
                    files={"file": (file_path.name, f, mime)},
                    data=_form(meta),
                    headers=headers,
                )
    except Exception as e:
        return UploadResult(RETRY, detail=f"{type(e).__name__}: {e}")

    url: Optional[str] = None
    if 200 <= r.status_code < 300:
        try:
            url = r.json().get("url")
        except Exception:
            url = None
    kind = classify(r.status_code, url)
    detail = f"HTTP {r.status_code}"
    if kind != OK:
        detail += f": {r.text[:200]}"
    return UploadResult(kind, url=url, status=r.status_code, detail=detail)


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


Uploader = Callable[[Path, UploadMeta], Awaitable[UploadResult]]


class UploadSpool:
    def __init__(
        self,
        directory: Path,
        uploader: Uploader,
        max_bytes: int,
        max_age_seconds: float,
        backoff_min: float,
        backoff_max: float,
        poll_seconds: float = 5.0,
        clock: Callable[[], float] = time.time,
    ):
        self.directory = Path(directory)
        self._upload = uploader
        self.max_bytes = max_bytes
        self.max_age_seconds = max_age_seconds
        self.backoff_min = backoff_min
        self.backoff_max = backoff_max
        self.poll_seconds = poll_seconds
        self._clock = clock
        self._task: Optional[asyncio.Task] = None
        self._wake = asyncio.Event()
        self.last_result: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------
    # Entries
    # ------------------------------------------------------------------

    def _backoff(self, attempts: int) -> float:
        return min(self.backoff_min * (2 ** max(0, attempts - 1)), self.backoff_max)

    @staticmethod
    def _write_sidecar(path: Path, data: Dict[str, Any]) -> None:
        # fsync file AND directory: on a Pi that loses power, an un-synced
        # rename can come back as a zero-length sidecar after the audio move
        # was already journaled.
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w") as f:
            f.write(json.dumps(data))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)

    def _recover_sidecar(self, audio: Path, sidecar: Path, why: str) -> Optional[Dict[str, Any]]:
        """Rebuild minimal metadata from the recording's own name
        (`YYYYMMDD_HHMMSS_<call_id>.flac`) rather than delete good audio. C2
        already has the talkgroup from call_start; only call_id is required."""
        parts = audio.name.split("_", 2)
        call_id = parts[2].split(".", 1)[0] if len(parts) == 3 else ""
        if not call_id:
            logger.error(f"Spooled audio {audio.name}: {why}, and no call_id in its name — dropping it.")
            self._remove(audio, sidecar)
            return None
        logger.error(f"Spooled audio {audio.name}: {why} — rebuilt metadata from the filename (call {call_id}).")
        try:
            spooled_at = audio.stat().st_mtime
        except OSError:
            spooled_at = self._clock()
        data = {**asdict(UploadMeta(call_id=call_id)), "spooled_at": spooled_at, "attempts": 1,
                "next_attempt_at": self._clock(), "last_error": why, "last_status": None}
        self._write_sidecar(sidecar, data)
        return data

    def add(self, file_path: Path, meta: UploadMeta, result: UploadResult) -> Optional[Path]:
        """Move a recording whose upload failed into the spool. Returns its new path."""
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            dest = self.directory / file_path.name
            sidecar = dest.with_name(dest.name + SIDECAR_SUFFIX)
            now = self._clock()
            self._write_sidecar(sidecar, {
                **asdict(meta),
                "spooled_at": now,
                "attempts": 1,
                "next_attempt_at": now + self._backoff(1),
                "last_error": result.detail,
                "last_status": result.status,
            })
            shutil.move(str(file_path), str(dest))
            _fsync_dir(self.directory)
        except Exception as e:
            logger.error(f"Could not spool recording for call {meta.call_id} ({e}) — audio is lost.")
            return None
        logger.warning(
            f"Upload of call {meta.call_id} failed ({result.detail}) — spooled for retry "
            f"({self.stats()['files']} waiting)."
        )
        self._wake.set()
        return dest

    def _entries(self) -> List[Tuple[Path, Path, Dict[str, Any]]]:
        """(audio, sidecar, data) oldest first. Cleans up half-written pairs."""
        if not self.directory.is_dir():
            return []
        entries = []
        sidecars = set()
        for sidecar in self.directory.glob(f"*{SIDECAR_SUFFIX}"):
            audio = sidecar.with_name(sidecar.name[: -len(SIDECAR_SUFFIX)])
            sidecars.add(audio.name)
            if not audio.exists():
                sidecar.unlink(missing_ok=True)
                continue
            try:
                data = json.loads(sidecar.read_text())
                if not data.get("call_id"):
                    raise ValueError("no call_id")
            except Exception as e:
                data = self._recover_sidecar(audio, sidecar, f"unreadable sidecar ({e})")
                if data is None:
                    continue
            entries.append((audio, sidecar, data))
        for audio in list(self.directory.iterdir()):
            if audio.is_file() and not audio.name.endswith((SIDECAR_SUFFIX, ".tmp")) and audio.name not in sidecars:
                sidecar = audio.with_name(audio.name + SIDECAR_SUFFIX)
                data = self._recover_sidecar(audio, sidecar, "no metadata sidecar")
                if data is not None:
                    entries.append((audio, sidecar, data))
        entries.sort(key=lambda e: (e[2].get("spooled_at", 0.0), e[0].name))
        return entries

    @staticmethod
    def _remove(audio: Path, sidecar: Path) -> None:
        # Audio first: a crash in between leaves a sidecar, which _entries() cleans.
        audio.unlink(missing_ok=True)
        sidecar.unlink(missing_ok=True)

    def stats(self) -> Dict[str, Any]:
        entries = self._entries()
        size = 0
        for audio, _, _ in entries:
            try:
                size += audio.stat().st_size
            except OSError:
                pass
        oldest = entries[0][2].get("spooled_at") if entries else None
        return {
            "files": len(entries),
            "bytes": size,
            "oldest_age_seconds": round(self._clock() - oldest, 1) if oldest else None,
            "last_result": self.last_result,
        }

    def enforce_caps(self) -> int:
        """Drop expired entries, then the oldest until under max_bytes. Returns how many went."""
        entries = self._entries()
        now = self._clock()
        dropped = 0
        kept = []
        for audio, sidecar, data in entries:
            if now - data.get("spooled_at", now) > self.max_age_seconds:
                logger.error(
                    f"Spooled call {data['call_id']} was never uploaded within "
                    f"{self.max_age_seconds / 3600:.0f}h ({data.get('attempts')} attempts, "
                    f"last: {data.get('last_error')}) — deleting its audio."
                )
                self._remove(audio, sidecar)
                dropped += 1
            else:
                kept.append((audio, sidecar, data))
        sizes = []
        for audio, sidecar, data in kept:
            try:
                sizes.append(audio.stat().st_size)
            except OSError:
                sizes.append(0)
        total = sum(sizes)
        i = 0
        while total > self.max_bytes and i < len(kept):
            audio, sidecar, data = kept[i]
            logger.error(
                f"Upload spool over {self.max_bytes} bytes — evicting the oldest, call {data['call_id']}."
            )
            self._remove(audio, sidecar)
            total -= sizes[i]
            dropped += 1
            i += 1
        return dropped

    # ------------------------------------------------------------------
    # Retry
    # ------------------------------------------------------------------

    async def drain_once(self, force: bool = False) -> int:
        """Attempt every due entry (every entry if `force`). Returns how many were uploaded."""
        self.enforce_caps()
        uploaded = 0
        for audio, sidecar, data in self._entries():
            if not force and data.get("next_attempt_at", 0.0) > self._clock():
                continue
            meta = UploadMeta(**{k: data.get(k) for k in UploadMeta.__dataclass_fields__})
            result = await self._upload(audio, meta)
            self.last_result = {"call_id": meta.call_id, "kind": result.kind, "status": result.status,
                                "detail": result.detail, "epoch": self._clock()}
            if result.kind == OK:
                logger.info(f"Spooled call {meta.call_id} uploaded after {data.get('attempts')} failed attempt(s).")
                self._remove(audio, sidecar)
                uploaded += 1
            elif result.kind == REJECT:
                logger.error(
                    f"C2 rejected spooled call {meta.call_id} ({result.detail}) — not retryable, deleting it."
                )
                self._remove(audio, sidecar)
            else:
                attempts = int(data.get("attempts", 1)) + 1
                data.update(attempts=attempts, last_error=result.detail, last_status=result.status,
                            next_attempt_at=self._clock() + self._backoff(attempts))
                self._write_sidecar(sidecar, data)
                logger.warning(
                    f"Retry {attempts} of spooled call {meta.call_id} failed ({result.detail}); next in "
                    f"{self._backoff(attempts):.0f}s."
                )
                # C2 is still down: the rest would fail the same way. Leave them
                # for their own timers rather than hammering it in one pass.
                break
        return uploaded

    def wake(self) -> None:
        """Retry everything now (e.g. a new API key just arrived)."""
        for audio, sidecar, data in self._entries():
            if data.get("next_attempt_at", 0.0) > self._clock():
                data["next_attempt_at"] = self._clock()
                self._write_sidecar(sidecar, data)
        self._wake.set()

    async def _run(self) -> None:
        # Startup drain: whatever survived a restart is attempted immediately.
        force = True
        while True:
            try:
                await self.drain_once(force=force)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Upload spool pass failed: {e}")
            force = False
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_seconds)
            except asyncio.TimeoutError:
                pass

    def start(self) -> None:
        if self._task is None or self._task.done():
            pending = len(self._entries())
            if pending:
                logger.info(f"Upload spool has {pending} recording(s) from before this start — draining.")
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
