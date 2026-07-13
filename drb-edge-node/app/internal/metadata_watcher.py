import asyncio
import uuid
from datetime import datetime, timezone
from typing import Optional, Callable, Awaitable
from app.internal.op25_client import op25_client
from app.internal.logger import logger

CallbackFn = Callable[[dict], Awaitable[None]]

HANG_THRESHOLD = 2   # polls before declaring a call ended (0.5s poll → 1s hang time)
POLL_INTERVAL = 0.5  # seconds


class MetadataWatcher:
    def __init__(self):
        self._running = False
        self._current_tgid: Optional[int] = None
        self._current_tgid_name: Optional[str] = None
        self._hang_counter: int = 0
        self._active_call_id: Optional[str] = None
        self._call_started_at: Optional[datetime] = None

        # Set these before calling start()
        self.on_call_start: Optional[CallbackFn] = None
        self.on_call_end: Optional[CallbackFn] = None

    async def start(self):
        self._running = True
        asyncio.create_task(self._poll_loop())
        logger.info("Metadata watcher started.")

    async def stop(self):
        self._running = False
        if self._active_call_id:
            await self._end_call()

    async def _poll_loop(self):
        while self._running:
            try:
                await self._tick()
            except Exception as e:
                logger.warning(f"Metadata poll error: {e}")
            await asyncio.sleep(POLL_INTERVAL)

    async def _tick(self):
        status = await op25_client.get_terminal_status()

        if not status:
            # OP25 not responding — hang-out any active call
            if self._active_call_id:
                self._hang_counter += 1
                if self._hang_counter >= HANG_THRESHOLD:
                    await self._end_call()
            return

        # OP25 terminal returns either a list of channels or a single dict
        channels = status if isinstance(status, list) else [status]
        active_tgid: Optional[int] = None
        active_meta: dict = {}

        for ch in channels:
            tgid = ch.get("tgid") or ch.get("tg_id")
            if tgid and str(tgid) not in ("0", "", "None"):
                active_tgid = int(tgid)
                active_meta = ch
                break

        if active_tgid:
            self._hang_counter = 0
            if self._current_tgid != active_tgid:
                # Talkgroup changed — close previous call and open a new one
                if self._active_call_id:
                    await self._end_call()
                self._current_tgid = active_tgid
                await self._start_call(active_tgid, active_meta)
        else:
            # No active talkgroup
            if self._active_call_id:
                self._hang_counter += 1
                if self._hang_counter >= HANG_THRESHOLD:
                    await self._end_call()

    async def _start_call(self, tgid: int, meta: dict):
        self._active_call_id = str(uuid.uuid4())
        self._call_started_at = datetime.now(timezone.utc)
        self._current_tgid_name = meta.get("tag") or meta.get("tgid_tag") or ""
        payload = {
            "call_id": self._active_call_id,
            "tgid": tgid,
            "tgid_name": self._current_tgid_name,
            "freq": meta.get("freq"),
            "srcaddr": meta.get("srcaddr"),
            "started_at": self._call_started_at.isoformat(),
        }
        logger.info(f"Call start: tgid={tgid} id={self._active_call_id}")
        if self.on_call_start:
            await self.on_call_start(payload)

    async def _end_call(self):
        if not self._active_call_id:
            return
        payload = {
            "call_id": self._active_call_id,
            "tgid": self._current_tgid,
            "tgid_name": self._current_tgid_name or "",
            "started_at": self._call_started_at.isoformat() if self._call_started_at else None,
            "ended_at": datetime.now(timezone.utc).isoformat(),
        }
        logger.info(f"Call end: id={self._active_call_id}")
        self._active_call_id = None
        self._current_tgid = None
        self._current_tgid_name = None
        self._hang_counter = 0
        self._call_started_at = None
        if self.on_call_end:
            await self.on_call_end(payload)

    @property
    def active_call_id(self) -> Optional[str]:
        return self._active_call_id

    @property
    def current_tgid(self) -> Optional[int]:
        return self._current_tgid

    @property
    def current_tgid_name(self) -> Optional[str]:
        return self._current_tgid_name

    @property
    def is_active(self) -> bool:
        return self._active_call_id is not None


metadata_watcher = MetadataWatcher()
