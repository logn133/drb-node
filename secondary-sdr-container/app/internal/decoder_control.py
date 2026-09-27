import json
import os
import signal
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from internal.logger import create_logger

LOGGER = create_logger(__name__)

# One decoder per mode, each on its own SDR (node-26#9). The node's secondary
# SDR *priority* (e.g. ["adsb", "ais"]) is applied by apply(): decoders start
# down the list until no free dongle is left, so every SDR the node has gets
# used and the ones beyond the list's reach simply aren't started. OP25 always
# keeps its own dongle — it is started first and never part of this list.
MODES = ("adsb", "ais")

# Which RTL-SDR index op25 holds is NOT fixed — on radio-box op25 had index 1
# and index 0 was free, so "op25 is always 0" was wrong. rtlsdr can't open a
# dongle another process has claimed, and the decoders exit within ~50ms when
# that happens, so _start_one() tries each index and keeps the first that
# stays up. op25 is never disturbed: a failed claim doesn't touch its dongle.
MAX_SDR_INDEX = 4
_STARTUP_GRACE_S = 2.0

_STATE_DIR = Path("/tmp/secondary_sdr")
ADSB_JSON_DIR = Path("/tmp/adsb")

# Live decoder handles. poll() is the only reliable liveness check: a decoder
# that dies on startup stays an unreaped zombie, and killpg(pgid, 0) still
# succeeds on a zombie.
_procs: Dict[str, subprocess.Popen] = {}
_indices: Dict[str, int] = {}
_lock = threading.Lock()

# AIS-catcher streams one JSON object per received message on stdout rather
# than writing a periodic snapshot file (readsb's approach) — so the
# current-vessel snapshot lives in memory, keyed by mmsi, kept warm by a
# background reader thread for as long as the decoder is alive.
_ais_vessels: Dict[str, Dict[str, Any]] = {}
_ais_lock = threading.Lock()


def _pgid_file(mode: str) -> Path:
    return _STATE_DIR / f"{mode}.pgid"


def _reap_orphans() -> None:
    """Kill decoders left behind by a previous API process (uvicorn --reload
    restarts this process, but decoders run in their own session and would
    otherwise keep holding their SDRs)."""
    if not _STATE_DIR.exists():
        return
    for f in _STATE_DIR.glob("*.pgid"):
        try:
            os.killpg(int(f.read_text().strip()), signal.SIGTERM)
            LOGGER.info(f"Stopped orphaned secondary decoder from {f.name}")
        except Exception:
            pass
        f.unlink(missing_ok=True)


_reap_orphans()


def _adsb_command(index: int) -> List[str]:
    ADSB_JSON_DIR.mkdir(parents=True, exist_ok=True)
    return [
        "/opt/readsb/readsb",
        "--net",
        "--device-type", "rtlsdr",
        "--device", str(index),
        "--write-json", str(ADSB_JSON_DIR),
        "--write-json-every", "1",
    ]


def _ais_command(index: int) -> List[str]:
    return [
        "/opt/AIS-catcher/build/AIS-catcher",
        f"-d:{index}",  # "-d <x>" would select by serial, not index
        "-o", "5",  # JSON Full: decoded fields (4 = sparse, "JSON" is rejected)
    ]


_COMMANDS = {"adsb": _adsb_command, "ais": _ais_command}


def _ais_reader(proc: subprocess.Popen) -> None:
    """
    Consume AIS-catcher's stdout, one JSON message per line, and keep the
    latest report per mmsi. Field names (mmsi/lat/lon/speed/course or
    heading/shipname or name) are believed correct from AIS-catcher's
    published JSON output docs but UNVERIFIED against a real capture in
    this session — same caveat as dump1090's aircraft.json mapping.
    Malformed/partial lines (e.g. static-data-only messages with no
    position) are skipped rather than raising, since dropping one line must
    never kill the reader thread.
    """
    if not proc.stdout:
        return
    for line in proc.stdout:
        try:
            msg = json.loads(line)
        except Exception:
            continue
        mmsi = msg.get("mmsi")
        if not mmsi:
            continue
        # AIS-catcher emits separate message TYPES per mmsi — static data
        # (name, no position) and position reports (lat/lon, no name) arrive
        # as distinct lines. Merge onto the existing entry, only overwriting
        # a field the new message actually carries, so a position-only
        # report doesn't blank out a name learned from an earlier message.
        name = (msg.get("shipname") or msg.get("name") or "").strip() or None
        heading = msg.get("heading") if msg.get("heading") is not None else msg.get("course")
        updates = {
            "mmsi": str(mmsi),
            "name": name,
            "lat": msg.get("lat"),
            "lon": msg.get("lon"),
            "speed_kt": msg.get("speed"),
            "heading_deg": heading,
        }
        with _ais_lock:
            existing = _ais_vessels.get(str(mmsi), {})
            for key, value in updates.items():
                if value is not None:
                    existing[key] = value
            _ais_vessels[str(mmsi)] = existing


def is_running(mode: str) -> bool:
    proc = _procs.get(mode)
    return proc is not None and proc.poll() is None


def running() -> List[str]:
    return [m for m in MODES if is_running(m)]


def _start_one(mode: str) -> bool:
    """Start one decoder on the first free SDR. False when none is free."""
    if mode not in _COMMANDS:
        raise ValueError(f"Unknown secondary SDR mode: {mode!r}")
    if is_running(mode):
        return True
    if mode == "ais":
        with _ais_lock:
            _ais_vessels.clear()

    needs_stdout = mode == "ais"
    for index in range(MAX_SDR_INDEX):
        if index in {_indices[m] for m in running() if m in _indices}:
            continue
        try:
            proc = subprocess.Popen(
                _COMMANDS[mode](index),
                preexec_fn=os.setsid,
                stdout=subprocess.PIPE if needs_stdout else None,
                text=True if needs_stdout else None,
                bufsize=1 if needs_stdout else -1,
            )
        except Exception as e:
            LOGGER.error(f"Failed to start secondary SDR decoder mode={mode!r}: {e}")
            return False
        try:
            proc.wait(timeout=_STARTUP_GRACE_S)
            LOGGER.info(f"Secondary SDR decoder mode={mode!r} could not use SDR index {index}, trying next")
            continue
        except subprocess.TimeoutExpired:
            pass
        if needs_stdout:
            threading.Thread(target=_ais_reader, args=(proc,), daemon=True).start()
        _procs[mode] = proc
        _indices[mode] = index
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        _pgid_file(mode).write_text(str(proc.pid))
        LOGGER.info(f"Started secondary SDR decoder mode={mode!r} on SDR index {index} pid={proc.pid}")
        return True

    LOGGER.info(f"Secondary SDR decoder mode={mode!r}: no free SDR left")
    return False


def _stop_one(mode: str) -> None:
    proc = _procs.pop(mode, None)
    _indices.pop(mode, None)
    if proc is not None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    _pgid_file(mode).unlink(missing_ok=True)


def start(mode: str) -> bool:
    with _lock:
        return _start_one(mode)


def stop(mode: Optional[str] = None) -> None:
    with _lock:
        for m in [mode] if mode else list(_procs):
            _stop_one(m)


def apply(priority: List[str]) -> List[str]:
    """Run decoders in priority order until SDRs run out; stop everything else.

    Unchanged when the running set is already the right prefix of the list, so
    re-applying the same priority (every restart/config push) is a no-op
    rather than a decoder restart.
    """
    for m in priority:
        if m not in _COMMANDS:
            raise ValueError(f"Unknown secondary SDR mode: {m!r}")
    with _lock:
        live = set(running())
        if live and live == set(priority[:len(live)]):
            # Already running the head of the list; only try to extend it.
            for m in priority[len(live):]:
                if not _start_one(m):
                    break
            return running()
        for m in list(_procs):
            _stop_one(m)
        for m in priority:
            if not _start_one(m):
                break
        return running()


def sdr_count() -> Optional[int]:
    """RTL-SDR dongles on USB, same heuristic as install.sh. This container has
    usbutils; op25's :stable image doesn't, so its /op25/devices can't answer."""
    try:
        out = subprocess.run(["lsusb"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    return sum(1 for line in out.splitlines() if "0bda:2838" in line or "0bda:2832" in line)


def status() -> Dict[str, Any]:
    live = running()
    return {
        "running": [{"mode": m, "sdr_index": _indices.get(m)} for m in live],
        "sdr_count": sdr_count(),
    }


def _altitude(a: Dict[str, Any]) -> Optional[int]:
    alt = a.get("alt_baro", a.get("altitude"))
    if alt == "ground":
        return 0
    return alt if isinstance(alt, (int, float)) else None


def _read_adsb_snapshot() -> List[Dict[str, Any]]:
    """
    Map readsb's aircraft.json (--write-json output) to the server's
    telemetry schema. readsb uses the dump1090-fa field names: alt_baro (int,
    or the string "ground"), gs, track. Older dump1090 forks used
    altitude/speed, kept as a fallback.
    """
    path = ADSB_JSON_DIR / "aircraft.json"
    try:
        raw = json.loads(path.read_text())
    except Exception:
        return []

    out = []
    for a in raw.get("aircraft", []):
        icao = a.get("hex")
        if not icao:
            continue
        out.append({
            "icao": icao.upper(),
            "callsign": (a.get("flight") or "").strip() or None,
            "lat": a.get("lat"),
            "lon": a.get("lon"),
            "altitude_ft": _altitude(a),
            "ground_speed_kt": a.get("gs", a.get("speed")),
            "track_deg": a.get("track"),
        })
    return out


def data() -> Dict[str, Any]:
    with _ais_lock:
        vessels = list(_ais_vessels.values()) if is_running("ais") else []
    return {
        "running": running(),
        "aircraft": _read_adsb_snapshot() if is_running("adsb") else [],
        "vessels": vessels,
    }
