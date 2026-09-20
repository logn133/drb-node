import json
import os
import signal
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from internal.logger import create_logger

LOGGER = create_logger(__name__)

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


def start(mode: str) -> bool:
    if is_running():
        stop()

    if mode == "adsb":
        cmd = _adsb_command()
    elif mode == "ais":
        raise ValueError("AIS mode is not wired yet (node-26#9) — only 'adsb' runs today.")
    else:
        raise ValueError(f"Unknown secondary SDR mode: {mode!r}")

    try:
        proc = subprocess.Popen(cmd, preexec_fn=os.setsid)
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
    return {"mode": mode, "aircraft": []}
