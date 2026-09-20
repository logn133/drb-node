import json
import os
import signal
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from internal.logger import create_logger

LOGGER = create_logger(__name__)

# AIS-catcher streams one JSON object per received message on stdout rather
# than writing a periodic snapshot file (dump1090's approach above) — so the
# current-vessel snapshot lives in memory, keyed by mmsi, kept warm by a
# background reader thread for as long as the subprocess we started is
# alive. Unlike the pgid-file state, this does NOT survive this API
# process restarting independently of its subprocess — acceptable since
# nothing here does that today.
_ais_vessels: Dict[str, Dict[str, Any]] = {}
_ais_lock = threading.Lock()

# The node's SECOND SDR, addressed by RTL-SDR index — not serial. op25 always
# claims index 0 (its DeviceConfig.args has no serial concept either, see
# op25-container/app/models.py). No hot-plug re-detection: if the two
# dongles' USB enumeration order changes, this claims the wrong one. Tracked
# as a real gap in node-26#9, not fixed here.
SECONDARY_SDR_DEVICE_INDEX = 1

_PGID_FILE = "/tmp/secondary_sdr.pgid"
_MODE_FILE = "/tmp/secondary_sdr.mode"
ADSB_JSON_DIR = Path("/tmp/adsb")


def _save_state(pgid: int, mode: str) -> None:
    Path(_PGID_FILE).write_text(str(pgid))
    Path(_MODE_FILE).write_text(mode)


def _read_pgid() -> Optional[int]:
    try:
        return int(Path(_PGID_FILE).read_text().strip())
    except Exception:
        return None


def _read_mode() -> Optional[str]:
    try:
        return Path(_MODE_FILE).read_text().strip()
    except Exception:
        return None


def is_running() -> bool:
    pgid = _read_pgid()
    if pgid is None:
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except OSError:
        return False


def _adsb_command() -> List[str]:
    ADSB_JSON_DIR.mkdir(parents=True, exist_ok=True)
    return [
        "/opt/dump1090/dump1090",
        "--net",
        "--device-index", str(SECONDARY_SDR_DEVICE_INDEX),
        "--write-json", str(ADSB_JSON_DIR),
        "--write-json-every", "1",
    ]


def _ais_command() -> List[str]:
    return [
        "/opt/AIS-catcher/build/AIS-catcher",
        "-d", str(SECONDARY_SDR_DEVICE_INDEX),
        "-o", "JSON",
    ]


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


def start(mode: str) -> bool:
    if is_running():
        stop()

    if mode == "adsb":
        cmd = _adsb_command()
    elif mode == "ais":
        cmd = _ais_command()
        with _ais_lock:
            _ais_vessels.clear()
    else:
        raise ValueError(f"Unknown secondary SDR mode: {mode!r}")

    try:
        needs_stdout = mode == "ais"
        proc = subprocess.Popen(
            cmd,
            preexec_fn=os.setsid,
            stdout=subprocess.PIPE if needs_stdout else None,
            text=True if needs_stdout else None,
            bufsize=1 if needs_stdout else -1,
        )
        if needs_stdout:
            threading.Thread(target=_ais_reader, args=(proc,), daemon=True).start()
        _save_state(proc.pid, mode)
        LOGGER.info(f"Started secondary SDR decoder mode={mode!r} pid={proc.pid}")
        return True
    except Exception as e:
        LOGGER.error(f"Failed to start secondary SDR decoder mode={mode!r}: {e}")
        return False


def stop() -> bool:
    pgid = _read_pgid()
    if pgid is None:
        return True
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        pass
    try:
        os.remove(_PGID_FILE)
    except OSError:
        pass
    try:
        os.remove(_MODE_FILE)
    except OSError:
        pass
    return True


def status() -> Dict[str, Any]:
    running = is_running()
    return {
        "status": "running" if running else "stopped",
        "mode": _read_mode() if running else None,
    }


def _read_adsb_snapshot() -> List[Dict[str, Any]]:
    """
    Map dump1090's aircraft.json (--write-json output) to the server's
    telemetry schema. Field names (hex/flight/lat/lon/altitude/speed/track)
    match dump1090's long-documented JSON format — UNVERIFIED against a real
    capture in this session, see the Dockerfile's caveat.
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
            "altitude_ft": a.get("altitude"),
            "ground_speed_kt": a.get("speed"),
            "track_deg": a.get("track"),
        })
    return out


def data() -> Dict[str, Any]:
    mode = _read_mode()
    if mode == "adsb":
        return {"mode": mode, "aircraft": _read_adsb_snapshot()}
    if mode == "ais":
        with _ais_lock:
            vessels = list(_ais_vessels.values())
        return {"mode": mode, "vessels": vessels}
    return {"mode": mode, "aircraft": [], "vessels": []}
