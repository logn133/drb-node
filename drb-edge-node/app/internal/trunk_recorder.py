"""
Trunk Recorder backend, edge-node side (node-26#45). Active only when
RECORDER_BACKEND=trunk-recorder.

    C2 SystemConfig --apply_config--> <tr_config_dir>/config.json (+ CSVs)
                                      trunk-recorder/run.sh restarts TR on change
    TR finishes a call --drb-upload.sh--> <tr_inbox_dir>/<stem>.wav, then <stem>.json
    ingest loop --> MQTT call_start --> /upload (call_recorder.upload_recording,
                    i.e. the node-26#44 spool) --> MQTT call_end
    TR simplestream (UDP) --LiveAudioBridge--> pacat --> drb_sink --> Discord

Why an inbox directory and not an HTTP endpoint: TR deletes the WAV/JSON as
soon as uploadScript returns (audioArchive/callLog false), so the script must
copy the files out synchronously anyway, and a file on the host disk survives
an edge-node restart where a POST to a down edge-node would not. The JSON is
moved in LAST, so its presence means the WAV is complete.

Restart safety: a call_id is a pure function of the JSON (tr_calls.parse_call),
and the WAV is renamed to `<YYYYmmdd_HHMMSS>_<call_id>.wav` (the name the spool
recovers a call_id from) before anything is sent. A crash at any step re-runs
the same call with the same id, so C2 overwrites instead of duplicating; a JSON
whose WAV is already gone was handed to the uploader and is just removed.
"""
import asyncio
import json
import math
import os
import re
import time
import wave
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from app.config import settings
from app.internal.logger import logger
from app.internal.tr_calls import TrCall, parse_call
from app.models import SystemConfig

CONFIG_FILE = "config.json"
STATUS_FILE = "drb-status.json"       # written by trunk-recorder/run.sh every few seconds
RESTART_REQUEST = "restart-request"   # run.sh restarts TR when this appears
STOP_REQUEST = "stop-request"         # run.sh keeps TR stopped while this exists

INBOX_POLL_SECONDS = 1.0
INGEST_CONCURRENCY = 4
# Hand-off is wav-then-json; a JSON whose WAV never shows is dropped after this.
ORPHAN_JSON_SECONDS = 60.0
# C2 creates the call doc from MQTT call_start, and only that message carries
# the talkgroup and start time. With the broker down, call_start is dropped
# (mqtt_manager buffers only call_end), so a call waits in the inbox this long
# for MQTT to come back before it is sent without one.
MQTT_WAIT_SECONDS = 600.0
# call_start must be written before /upload stamps audio_gcs_uri: C2's
# _on_call_start writes the doc with merge=False and would erase it
# (server-26#303; drop this lead once C2 makes call_start create-only).
CALL_START_LEAD_SECONDS = 1.0
# A WAV with no JSON this old is a hand-off drb-upload.sh never finished.
STRAY_WAV_SECONDS = 3600.0
# How long radio_backend waits for run.sh to (re)start TR after a request: it
# polls every 5 s and may spend 15 s stopping the old process.
RESTART_WAIT_SECONDS = 30.0
RESTART_POLL_SECONDS = 1.0
# The name process() renames a claimed WAV to; never swept (its JSON is what
# drives it, and os.replace keeps TR's old mtime).
CANONICAL_WAV_RE = re.compile(r"^\d{8}_\d{6}_[0-9a-f-]{36}\.wav$")
# run.sh rewrites its status every 5 s; past this the supervisor is gone.
STATUS_STALE_SECONDS = 20.0
LOG_TAIL_BYTES = 64 * 1024
# TR logs this for every trunked system every few seconds (main.cpp
# check_message_count). Exact wording unverified against a live log.
DECODE_RATE_RE = re.compile(r"Decode Rate:\s*([0-9.]+)\s*/\s*sec", re.IGNORECASE)


def enabled() -> bool:
    return settings.recorder_backend == "trunk-recorder"


def config_dir() -> Path:
    return Path(settings.tr_config_dir or (Path(settings.config_path) / "trunk-recorder"))


def inbox_dir() -> Path:
    return Path(settings.tr_inbox_dir or (Path(settings.recordings_path) / "tr-inbox"))


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def wav_seconds(path: Path) -> Optional[float]:
    """Length of a WAV from its header; None if it can't be read."""
    try:
        with wave.open(str(path), "rb") as w:
            rate = w.getframerate()
            return w.getnframes() / rate if rate else None
    except (OSError, EOFError, wave.Error):
        return None


def canonical_wav_name(call: TrCall) -> str:
    stamp = datetime.fromtimestamp(call.start_epoch).strftime("%Y%m%d_%H%M%S")
    return f"{stamp}_{call.call_id}.wav"


class TrunkRecorder:
    def __init__(self):
        self.config_error: Optional[str] = None
        self.calls_ingested = 0
        self.encrypted_skipped = 0
        self.last_call_epoch: Optional[float] = None
        self.last_call: Optional[Dict[str, Any]] = None
        self._task: Optional[asyncio.Task] = None
        self._inflight: Set[str] = set()
        self._sem = asyncio.Semaphore(INGEST_CONCURRENCY)
        self.live = LiveAudioBridge()

    # ------------------------------------------------------------------
    # Config + supervisor requests
    # ------------------------------------------------------------------

    async def apply_config(self, config: SystemConfig) -> bool:
        """Write TR's config for `config`. run.sh restarts TR when config.json
        changes; a restart request covers a CSV-only change."""
        from app.internal.config_manager import load_node_config
        from app.internal.sdr_settings import op25_serial
        from app.internal.secondary_sdr_client import secondary_sdr_client
        from app.internal.tr_config import build_tr_config, live_sample_rate

        node_cfg = load_node_config()
        # TR takes the dongle OP25 would have (pin or first detected), so the
        # secondary decoders' reservation logic is unchanged.
        devs = await secondary_sdr_client.devices()
        serial = op25_serial(node_cfg, devs)
        if serial and devs and sum(d["serial"] == serial for d in devs) > 1:
            serial = None  # two dongles share it: by-serial would be ambiguous
        try:
            tr, files = build_tr_config(
                config.type, config.name, config.config,
                device_serial=serial, ppm=node_cfg.ppm_override,
                sample_rate=settings.tr_sample_rate, gain=settings.tr_gain,
                digital_recorders=settings.tr_digital_recorders, min_duration=settings.tr_min_duration,
                squelch_db=settings.tr_squelch_db, live_port=settings.tr_live_port,
                center_override=settings.tr_center_hz,
            )
        except ValueError as e:
            self.config_error = f"System {config.name!r}: {e}"
            logger.error(f"Trunk Recorder config not written — {self.config_error}")
            return False
        try:
            d = config_dir()
            d.mkdir(parents=True, exist_ok=True)
            (d / "logs").mkdir(exist_ok=True)
            # CSVs first: run.sh restarts on config.json, which must never
            # point at a file that isn't there yet.
            for name, text in files.items():
                _atomic_write(d / name, text)
            _atomic_write(d / CONFIG_FILE, json.dumps(tr, indent=2))
        except OSError as e:
            self.config_error = f"Couldn't write {config_dir()}: {e}"
            logger.error(self.config_error)
            return False
        self.config_error = None
        self.live.sample_rate = live_sample_rate(config.type, config.config)
        logger.info(f"Trunk Recorder config written for {config.name} (SDR {serial or 'rtl=0'}).")
        return True

    def request_restart(self) -> bool:
        try:
            d = config_dir()
            d.mkdir(parents=True, exist_ok=True)
            (d / STOP_REQUEST).unlink(missing_ok=True)
            (d / RESTART_REQUEST).write_text(str(time.time()))
            return True
        except OSError as e:
            logger.error(f"Couldn't ask Trunk Recorder to restart: {e}")
            return False

    async def wait_started_since(self, since: float, timeout: Optional[float] = None) -> Optional[str]:
        """Wait until run.sh reports TR running with started_at >= `since`.

        Callers hand the spare dongles to the secondary decoders right after a
        start; returning before TR has actually reopened its SDR would let a
        decoder take it. None when started, else why not.
        """
        timeout = RESTART_WAIT_SECONDS if timeout is None else timeout
        deadline = time.time() + timeout
        while True:
            st = self.supervisor_status()
            if st and not st["stale"] and st.get("state") == "running" \
                    and float(st.get("started_at") or 0) >= math.floor(since):
                return None
            if time.time() >= deadline:
                break
            await asyncio.sleep(RESTART_POLL_SECONDS)
        if st is None:
            return "The trunk-recorder service isn't running (is COMPOSE_PROFILES=trunk-recorder set?)."
        return f"Trunk Recorder didn't restart within {timeout:.0f}s (state: {st.get('state')})."

    def request_stop(self) -> bool:
        try:
            d = config_dir()
            d.mkdir(parents=True, exist_ok=True)
            (d / STOP_REQUEST).write_text(str(time.time()))
            return True
        except OSError as e:
            logger.error(f"Couldn't ask Trunk Recorder to stop: {e}")
            return False

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def supervisor_status(self, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
        now = time.time() if now is None else now
        try:
            data = json.loads((config_dir() / STATUS_FILE).read_text())
        except (OSError, ValueError):
            return None
        data["age_seconds"] = round(now - float(data.get("epoch") or 0), 1)
        data["stale"] = data["age_seconds"] > STATUS_STALE_SECONDS
        return data

    def log_facts(self, now: Optional[float] = None) -> Dict[str, Any]:
        now = time.time() if now is None else now
        logs = config_dir() / "logs"
        try:
            files = [p for p in logs.iterdir() if p.is_file()]
        except OSError:
            files = []
        if not files:
            return {"log_age_seconds": None, "decode_rate": None}
        newest = max(files, key=lambda p: p.stat().st_mtime)
        age = now - newest.stat().st_mtime
        rate = None
        try:
            with open(newest, "rb") as f:
                f.seek(max(0, newest.stat().st_size - LOG_TAIL_BYTES))
                tail = f.read().decode(errors="replace")
            found = DECODE_RATE_RE.findall(tail)
            rate = float(found[-1]) if found else None
        except OSError:
            pass
        return {"log_age_seconds": round(age, 1), "decode_rate": rate}

    def log_tail(self, lines: int = 80) -> List[str]:
        """Last lines of TR's newest log file, for the dashboard's log panel."""
        try:
            files = [p for p in (config_dir() / "logs").iterdir() if p.is_file()]
            if not files:
                return []
            newest = max(files, key=lambda p: p.stat().st_mtime)
            with open(newest, "rb") as f:
                f.seek(max(0, newest.stat().st_size - LOG_TAIL_BYTES))
                text = f.read().decode(errors="replace")
        except OSError:
            return []
        return text.splitlines()[-max(1, min(int(lines), 500)):]

    def sweep_stray_wavs(self, now: Optional[float] = None) -> int:
        """Delete WAVs whose JSON never arrived (drb-upload.sh died between
        its two moves). Canonical names (already claimed by a JSON)
        are never touched, so only true strays go."""
        now = time.time() if now is None else now
        removed = 0
        try:
            jsons = {p.stem for p in inbox_dir().glob("*.json")}
            for wav in inbox_dir().glob("*.wav"):
                if wav.stem in jsons or CANONICAL_WAV_RE.match(wav.name) \
                        or now - wav.stat().st_mtime < STRAY_WAV_SECONDS:
                    continue
                logger.error(f"Trunk Recorder WAV {wav.name} has had no call JSON for an hour — deleting it.")
                wav.unlink(missing_ok=True)
                removed += 1
        except OSError:
            pass
        return removed

    def inbox_backlog(self) -> int:
        try:
            return sum(1 for _ in inbox_dir().glob("*.json"))
        except OSError:
            return 0

    def health_facts(self) -> Dict[str, Any]:
        return {
            "status": self.supervisor_status(),
            **self.log_facts(),
            "config_error": self.config_error,
            "inbox_backlog": self.inbox_backlog(),
            "calls_ingested": self.calls_ingested,
            "encrypted_skipped": self.encrypted_skipped,
            "last_call_epoch": self.last_call_epoch,
            "last_call": self.last_call,
            "live": self.live.stats(),
        }

    # ------------------------------------------------------------------
    # Ingest
    # ------------------------------------------------------------------

    async def start(self) -> None:
        inbox_dir().mkdir(parents=True, exist_ok=True)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())
        await self.live.start()

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.live.stop()

    async def _run(self) -> None:
        backlog = self.inbox_backlog()
        if backlog:
            logger.info(f"Trunk Recorder inbox has {backlog} call(s) from before this start — sending.")
        last_sweep = 0.0
        while True:
            try:
                await self.scan_once()
                if time.time() - last_sweep > 300:
                    last_sweep = time.time()
                    self.sweep_stray_wavs()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Trunk Recorder inbox pass failed: {e}")
            await asyncio.sleep(INBOX_POLL_SECONDS)

    async def scan_once(self, wait: bool = False) -> int:
        """Start a task per ready call. Returns how many were started; with
        `wait`, also waits for them (tests)."""
        from app.internal.mqtt_manager import mqtt_manager
        try:
            ready = sorted(inbox_dir().glob("*.json"), key=lambda p: p.stat().st_mtime)
        except OSError:
            return 0
        started: List[asyncio.Task] = []
        now = time.time()
        for path in ready:
            if path.name in self._inflight:
                continue
            try:
                age = now - path.stat().st_mtime
            except OSError:
                continue
            if not mqtt_manager.is_connected and age < MQTT_WAIT_SECONDS:
                continue
            self._inflight.add(path.name)
            started.append(asyncio.create_task(self._guarded(path)))
        if wait and started:
            await asyncio.gather(*started)
        return len(started)

    async def _guarded(self, path: Path) -> None:
        try:
            async with self._sem:
                await self.process(path)
        except Exception as e:
            logger.error(f"Trunk Recorder call {path.name} failed: {e}")
        finally:
            self._inflight.discard(path.name)

    async def process(self, json_path: Path) -> None:
        from app.internal.call_recorder import call_recorder
        from app.internal.config_manager import load_node_config
        from app.internal.mqtt_manager import mqtt_manager

        try:
            data = json.loads(json_path.read_text())
            call = parse_call(data, settings.node_id, json_path.stem)
        except (OSError, ValueError) as e:
            logger.error(f"Trunk Recorder call {json_path.name} is unreadable ({e}) — dropping it.")
            json_path.with_suffix(".wav").unlink(missing_ok=True)
            json_path.unlink(missing_ok=True)
            return

        raw_wav = json_path.with_suffix(".wav")
        wav = json_path.parent / canonical_wav_name(call)
        if raw_wav.exists():
            os.replace(raw_wav, wav)
        if not wav.exists():
            age = time.time() - json_path.stat().st_mtime
            if age > ORPHAN_JSON_SECONDS:
                # Either TR's hand-off broke, or a previous pass already handed
                # this WAV to the uploader (which deletes or spools it) and
                # died before removing the JSON. Either way nothing to send.
                # Still close it in C2: if a previous pass uploaded and died
                # before this point, the doc would otherwise stay "active".
                # _on_call_end merges, so a repeat is harmless.
                logger.warning(f"Trunk Recorder call {call.call_id}: no audio beside {json_path.name} — removing it.")
                await mqtt_manager.publish_metadata("call_end", {**call.end_payload(), "audio_skipped": "no_audio"})
                json_path.unlink(missing_ok=True)
            return

        await mqtt_manager.publish_metadata("call_start", call.start_payload())
        seconds = wav_seconds(wav)
        end = call.end_payload(seconds)

        if call.encrypted:
            # Encrypted P25 voice decodes to noise: nothing for Whisper. The
            # call is still reported so C2 sees the activity.
            wav.unlink(missing_ok=True)
            end["audio_skipped"] = "encrypted"
            self.encrypted_skipped += 1
        elif settings.c2_url:
            await asyncio.sleep(CALL_START_LEAD_SECONDS)
            node_cfg = load_node_config()
            url = await call_recorder.upload_recording(
                wav, call.call_id,
                talkgroup_id=call.tgid, talkgroup_name=call.tgid_name or None,
                system_id=node_cfg.assigned_system_id,
                audio_start_epoch=call.start_epoch, audio_end_epoch=call.audio_end_epoch(seconds),
                extra=call.upload_extra(),
            )
            if url:
                end["audio_url"] = url
            else:
                kind = (call_recorder.spool.last_result or {}).get("kind")
                end["audio_upload"] = {"retry": "spooled", "lost": "lost"}.get(kind, "rejected")
        else:
            wav.unlink(missing_ok=True)

        await mqtt_manager.publish_metadata("call_end", end)
        json_path.unlink(missing_ok=True)
        self.calls_ingested += 1
        self.last_call_epoch = time.time()
        self.last_call = {"call_id": call.call_id, "tgid": call.tgid, "tgid_name": call.tgid_name,
                          "units": call.units, "duration": round(call.stop_epoch - call.start_epoch, 1),
                          "encrypted": call.encrypted, "emergency": call.emergency}
        logger.info(
            f"TR call {call.call_id}: TG {call.tgid} {call.tgid_name!r}, "
            f"{call.stop_epoch - call.start_epoch:.1f}s, units {call.units or '-'}"
            + (" [EMERGENCY]" if call.emergency else "") + (" [encrypted]" if call.encrypted else "")
        )


class LiveAudioBridge:
    """TR simplestream (UDP, s16le mono, 4-byte little-endian TGID prefix) ->
    `pacat` into drb_sink, so Discord keeps reading drb_sink.monitor unchanged.

    TR records every talkgroup at once and streams them all to one port;
    interleaving two calls' packets would garble both. So this follows ONE
    talkgroup — the first heard — until it has been quiet for HOLD_SECONDS,
    like the one-voice-channel scanner OP25 was. Recording is unaffected:
    every call is still recorded and uploaded in full.
    """

    HOLD_SECONDS = 2.0
    RESPAWN_SECONDS = 5.0
    MAX_BUFFERED_BYTES = 64 * 1024  # ~4 s at 8 kHz; beyond this pacat is stuck, drop

    def __init__(self):
        self.sample_rate = 8000
        self.current_tgid: Optional[int] = None
        self.last_packet_epoch: Optional[float] = None
        self._last_current_epoch = 0.0
        self.packets = 0
        self.dropped = 0
        self._transport = None
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._proc_rate: Optional[int] = None
        self._spawn_after = 0.0
        self._spawning = False

    def stats(self) -> Dict[str, Any]:
        return {"current_tgid": self.current_tgid, "last_packet_epoch": self.last_packet_epoch,
                "packets": self.packets, "dropped": self.dropped,
                "pacat_running": self._proc is not None and self._proc.returncode is None}

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        bridge = self

        class _Proto(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                bridge.feed(data)

        try:
            self._transport, _ = await loop.create_datagram_endpoint(
                _Proto, local_addr=("127.0.0.1", settings.tr_live_port),
            )
            logger.info(f"Trunk Recorder live audio: listening on 127.0.0.1:{settings.tr_live_port}.")
        except OSError as e:
            logger.error(f"Trunk Recorder live audio disabled — can't bind UDP {settings.tr_live_port}: {e}")

    async def stop(self) -> None:
        if self._transport:
            self._transport.close()
            self._transport = None
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
        self._proc = None

    def select(self, tgid: int, now: float) -> bool:
        """Follow-one-talkgroup policy. True if this packet should be played."""
        if self.current_tgid is None or now - self._last_current_epoch > self.HOLD_SECONDS:
            self.current_tgid = tgid
        if tgid != self.current_tgid:
            return False
        self._last_current_epoch = now
        return True

    def feed(self, data: bytes, now: Optional[float] = None) -> Optional[bytes]:
        """One datagram. Returns the PCM that was (or would be) played."""
        if len(data) <= 4:
            return None
        now = time.time() if now is None else now
        tgid = int.from_bytes(data[:4], "little")
        self.packets += 1
        self.last_packet_epoch = now
        if not self.select(tgid, now):
            return None
        pcm = data[4:]
        self._write(pcm)
        return pcm

    def _write(self, pcm: bytes) -> None:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            self._ensure_pacat()
            return
        if self._proc_rate != self.sample_rate:
            # A config change, not a failure: no respawn throttle.
            self._spawn_after = 0.0
            self._ensure_pacat()
            return
        stdin = proc.stdin
        if stdin is None or stdin.is_closing():
            return
        try:
            if stdin.transport.get_write_buffer_size() > self.MAX_BUFFERED_BYTES:
                self.dropped += 1
                return
            stdin.write(pcm)
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            self._proc = None  # pacat died; respawned on the next packet

    def _ensure_pacat(self) -> None:
        if self._spawning or time.time() < self._spawn_after:
            return
        self._spawning = True
        self._spawn_after = time.time() + self.RESPAWN_SECONDS
        try:
            asyncio.get_running_loop().create_task(self._spawn())
        except RuntimeError:
            self._spawning = False

    async def _spawn(self) -> None:
        try:
            if self._proc and self._proc.returncode is None:
                self._proc.terminate()
            sink = settings.pulse_source.removesuffix(".monitor")
            self._proc = await asyncio.create_subprocess_exec(
                "pacat", "--playback", "--raw", "--format=s16le", f"--rate={self.sample_rate}",
                "--channels=1", f"--device={sink}", "--latency-msec=250", "--client-name=drb-trunk-recorder",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            self._proc_rate = self.sample_rate
        except Exception as e:
            logger.error(f"Trunk Recorder live audio: pacat failed to start ({e}); retrying.")
            self._proc = None
        finally:
            self._spawning = False


trunk_recorder = TrunkRecorder()
