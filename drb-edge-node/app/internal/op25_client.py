import httpx
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from app.config import settings
from app.internal.logger import logger

# The OP25 HTTP terminal answers a single "update" command with a LIST of
# messages, each tagged with a `json_type`. We care about two of them:
#
#   channel_update — current receiver state. `channels` holds the channel ids and
#                    each id is also a top-level key holding that channel's dict
#                    (freq/tgid/tag/srcaddr/svcopts/hold_tgid/…).
#
#   trunk_update   — per-system trunking state keyed "0", "1", …; each carries
#                    `last_tsbk` (OP25's time.time() of the last control-channel
#                    message decoded) — the only direct proof OP25 is DECODING,
#                    not merely running. Surfaced on the dashboard only.
#
#   call_log       — an EVENT QUEUE, not a snapshot. `log` holds entries appended
#                    by tk_p25.log_call() at channel-grant time, each stamped with
#                    OP25's own time.time(). get_call_log() DRAINS the deque, so
#                    every entry is delivered exactly once and a missed poll loses
#                    it forever. The deque is capped at CALL_LOG_MAX_LEN = 10, so
#                    the consumer must keep up.
#
# Everything else (rx_update, terminal_config, …) is ignored.
TERMINAL_UPDATE_COMMAND = [{"command": "update", "arg1": 0, "arg2": 0}]


@dataclass
class TerminalUpdate:
    """One decoded poll of the OP25 HTTP terminal."""

    channels: List[Dict[str, Any]] = field(default_factory=list)
    call_log: List[Dict[str, Any]] = field(default_factory=list)
    trunk: List[Dict[str, Any]] = field(default_factory=list)


class OP25Client:
    def __init__(self):
        self.api_url = settings.op25_api_url
        self.terminal_url = settings.op25_terminal_url

    async def start(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(f"{self.api_url}/op25/start")
                r.raise_for_status()
                return True
        except Exception as e:
            logger.error(f"OP25 start failed: {e}")
            return False

    async def start_with_reason(self) -> Optional[str]:
        """Start OP25; None on success, else a human-readable reason."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(f"{self.api_url}/op25/start")
        except Exception as e:
            logger.error(f"OP25 start failed: {e}")
            return "The op25 container isn't answering."
        if r.is_success:
            return None
        try:
            detail = r.json().get("detail")
        except Exception:
            detail = None
        logger.error(f"OP25 start failed: HTTP {r.status_code} {detail or r.text[:200]}")
        return detail or f"op25 container returned HTTP {r.status_code}."

    async def stop(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(f"{self.api_url}/op25/stop")
                r.raise_for_status()
                return True
        except Exception as e:
            logger.error(f"OP25 stop failed: {e}")
            return False

    async def status(self) -> Optional[Dict[str, Any]]:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(f"{self.api_url}/op25/status")
                r.raise_for_status()
                return r.json()
        except Exception as e:
            logger.error(f"OP25 status failed: {e}")
            return None

    async def log(self, lines: int = 80) -> Optional[List[str]]:
        """Tail of multi_rx/liquidsoap output, kept by the op25 container."""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(f"{self.api_url}/op25/log", params={"lines": lines})
                r.raise_for_status()
                return r.json().get("lines", [])
        except Exception as e:
            logger.error(f"OP25 log fetch failed: {e}")
            return None

    async def devices(self) -> Optional[Dict[str, Any]]:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(f"{self.api_url}/op25/devices")
                r.raise_for_status()
                return r.json()
        except Exception as e:
            logger.error(f"OP25 device enumeration failed: {e}")
            return None

    async def generate_config(self, config: Dict[str, Any]) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(f"{self.api_url}/op25/generate-config", json=config)
                r.raise_for_status()
        except Exception as e:
            logger.error(f"OP25 generate-config failed: {e}")
            return False
        # Every generated config opens its dongle by serial, never "first
        # found" (node-26#11) — here so no generation path can skip it.
        from app.internal.sdr_settings import pin_op25_device
        await pin_op25_device()
        return True

    async def poll_terminal(self) -> Optional[TerminalUpdate]:
        """
        Poll the OP25 HTTP terminal once and decode every message we understand.

        Returns None only when OP25 is unreachable / returned garbage — callers
        use that to distinguish "no traffic" from "no OP25".
        """
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                r = await client.post(self.terminal_url, json=TERMINAL_UPDATE_COMMAND)
                r.raise_for_status()
                return parse_terminal_messages(r.json())
        except Exception:
            return None

    async def get_terminal_status(self) -> Optional[Dict[str, Any]]:
        """
        Compatibility shim: the first channel's state dict, as this used to return.

        Prefer poll_terminal() — this discards the call_log, which is the only
        source of exact call-start timestamps.
        """
        update = await self.poll_terminal()
        if not update or not update.channels:
            return None
        return update.channels[0]


def parse_terminal_messages(messages: Any) -> TerminalUpdate:
    """
    Decode an OP25 terminal response into channel state + call-log events.

    Deliberately permissive: the response may be a bare dict instead of a list,
    may contain json_type values we have never seen, and individual entries may
    be malformed. Anything unrecognised is skipped rather than raising, because
    dropping a whole poll would drop call_log events that are never re-sent.
    """
    update = TerminalUpdate()

    if isinstance(messages, dict):
        messages = [messages]
    if not isinstance(messages, list):
        return update

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        json_type = msg.get("json_type")

        if json_type == "channel_update":
            for chan_id in msg.get("channels") or []:
                channel = msg.get(str(chan_id))
                if isinstance(channel, dict):
                    update.channels.append(channel)

        elif json_type == "trunk_update":
            for key, system in msg.items():
                if key != "json_type" and isinstance(system, dict):
                    update.trunk.append(system)

        elif json_type == "call_log":
            for entry in msg.get("log") or []:
                if isinstance(entry, dict):
                    update.call_log.append(entry)

    return update


op25_client = OP25Client()
