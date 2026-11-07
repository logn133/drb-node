"""
Trunk Recorder config.json (+ CSVs) from the C2 system config (node-26#45).

Same input as radio_config.build_generate_payload — the SystemConfig C2 pushes —
so a node can switch backends without anyone re-entering its system:

    P25 trunked       -> system type "p25", control_channels, talkgroupsFile
    P25 conventional  -> "conventionalP25", channelFile
    Analog            -> "conventional",    channelFile, squelch

Schema per Trunk Recorder v5.2.1 docs/CONFIGURE.md. Pure functions: the I/O is
in trunk_recorder.py. Unverified against a radio — see the notes inline.

Conventional channels are numbered with conventional.channel_tgid (frequency in
100 Hz units), the same synthetic talkgroup the OP25 path reports, so C2 names
a TR call and an OP25 call on the same channel identically.
"""
import csv
import io
from typing import Any, Dict, List, Optional, Tuple

from app.internal.logger import logger
from app.internal.conventional import channel_tgid
from app.internal.radio_config import (
    ANALOG, CONVENTIONAL, DMR, TRUNKED, analog_frequency, conventional_channels, system_mode, to_hz,
)

# Paths INSIDE the trunk-recorder container (see docker-compose.yml).
TR_APP_DIR = "/app"                         # tr_config_dir is mounted here
TR_UPLOAD_SCRIPT = "/drb/drb-upload.sh"     # ./trunk-recorder, read-only
TR_CAPTURE_DIR = "/recordings/tr-capture"   # ./recordings
TR_TEMP_DIR = "/dev/shm"                    # TR's recommended tmpfs (shm_size in compose)
TALKGROUPS_FILE = "talkgroups.csv"
CHANNELS_FILE = "channels.csv"
SHORT_NAME = "drb"

# TR splits a call longer than this; mirrors call_recorder.MAX_RECORDING_SECONDS.
MAX_DURATION_SECONDS = 600
# Seconds of control-channel silence before TR closes a call (TR's default).
CALL_TIMEOUT_SECONDS = 3

# Of an RTL-SDR's sample rate, the share that is usable passband (anti-alias
# roll-off eats the edges). Channels must sit inside +-(rate * share / 2).
USABLE_BANDWIDTH_SHARE = 0.8
# The RTL-SDR's DC spike sits on the centre frequency; keep channels off it.
DC_SPIKE_GUARD_HZ = 25_000
CENTER_NUDGES_HZ = (0, 150_000, -150_000, 300_000, -300_000)


def _fits(center: int, freqs: List[int], half_band: float) -> bool:
    return all(abs(f - center) <= half_band for f in freqs)


def widest_cover(freqs: List[int], rate: int) -> List[int]:
    """The largest run of frequencies one SDR at `rate` can hold at once
    (ties: the lowest). A trunked system lists every control channel it might
    use, but TR needs only one live at a time, so a list wider than the SDR is
    cut down to what fits instead of refusing the whole system."""
    half = rate * USABLE_BANDWIDTH_SHARE / 2
    fs = sorted(set(freqs))
    best: List[int] = []
    for i, lo in enumerate(fs):
        run = [f for f in fs[i:] if f - lo <= 2 * half]
        if len(run) > len(best):
            best = run
    return best


def choose_center(freqs: List[int], rate: int, override: Optional[int] = None) -> int:
    """A centre frequency that puts every channel in the passband and none on
    the DC spike. ValueError if they cannot all fit on one SDR."""
    if not freqs:
        raise ValueError("No frequencies to tune to.")
    half = rate * USABLE_BANDWIDTH_SHARE / 2
    if override:
        if not _fits(override, freqs, half):
            raise ValueError(f"TR_CENTER_HZ {override} leaves a channel outside the SDR's {2 * half / 1e6:.1f} MHz.")
        return int(override)
    lo, hi = min(freqs), max(freqs)
    if hi - lo > 2 * half:
        raise ValueError(
            f"Channels span {(hi - lo) / 1e6:.3f} MHz, more than one SDR at {rate / 1e6:.1f} Msps covers "
            f"({2 * half / 1e6:.2f} MHz). Trim the channel list or raise TR_SAMPLE_RATE."
        )
    mid = int(round((lo + hi) / 2))
    for nudge in CENTER_NUDGES_HZ:
        c = mid + nudge
        if _fits(c, freqs, half) and all(abs(f - c) >= DC_SPIKE_GUARD_HZ for f in freqs):
            return c
    return mid  # channels packed around the middle: accept the spike over not fitting


def _csv(header: List[str], rows: List[List[Any]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue()


def talkgroups_csv(talkgroups: List[Dict[str, Any]]) -> str:
    """TR talkgroupsFile: header row required, "Decimal" first (CONFIGURE.md)."""
    rows = []
    for tg in talkgroups:
        if tg.get("id") is None:
            continue
        name = str(tg.get("name") or "").strip()
        rows.append([int(tg["id"]), "D", name, name, "", ""])
    return _csv(["Decimal", "Mode", "Alpha Tag", "Description", "Tag", "Category"], rows)


def channels_csv(channels: List[Dict[str, Any]], squelch: Optional[float] = None) -> str:
    """TR channelFile: header required, "TG Number" first; Frequency in Hz
    (no decimal point = Hz). Per-channel Squelch overrides the system's."""
    header = ["TG Number", "Frequency", "Alpha Tag", "Description"]
    if squelch is not None:
        header.append("Squelch")
    rows = []
    for ch in channels:
        name = ch.get("name") or f"{ch['frequency'] / 1e6:.4f} MHz"
        row = [channel_tgid(ch["frequency"]), ch["frequency"], name, name]
        if squelch is not None:
            row.append(squelch)
        rows.append(row)
    return _csv(header, rows)


def _analog_channels(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every analog channel the config lists (OP25 only ever used the first)."""
    out = []
    for raw in (config or {}).get("channels") or []:
        if isinstance(raw, dict) and raw.get("freq") not in (None, ""):
            out.append({"frequency": to_hz(raw["freq"]), "name": str(raw.get("name") or "").strip()})
    if not out:
        freq = analog_frequency(config)
        if freq:
            out.append({"frequency": freq, "name": ""})
    return out


def _analog_squelch(config: Dict[str, Any], default_db: float) -> float:
    """TR's squelch is an absolute power level — OP25's "power" mode. Its
    squelch_threshold carries over; noise/voice squelch_db has no TR
    equivalent, so those fall back to the default (which needs site tuning)."""
    value = (config or {}).get("squelch_threshold")
    if value is None or (isinstance(value, str) and not value.strip()):
        return float(default_db)
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"squelch_threshold {value!r} isn't a number.")


def build_tr_config(
    system_type: str,
    name: str,
    config: Dict[str, Any],
    *,
    device_serial: Optional[str],
    ppm: Optional[float],
    sample_rate: int,
    gain: float,
    digital_recorders: int,
    min_duration: float,
    squelch_db: float,
    live_port: int,
    center_override: Optional[int] = None,
) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """SystemConfig fields -> (config.json dict, {filename: contents} to write beside it).

    ValueError when the system can't be expressed (DMR, no channels, too wide).
    """
    config = config or {}
    mode = system_mode(system_type, config)
    files: Dict[str, str] = {}
    # Kept per system, not globally: audioArchive/callLog false makes TR delete
    # the wav/json after uploadScript returns, which is why drb-upload.sh
    # copies them out before it exits.
    system: Dict[str, Any] = {
        "shortName": SHORT_NAME,
        "uploadScript": TR_UPLOAD_SCRIPT,
        "audioArchive": False,
        "callLog": False,
        # WAV, not TR's 32k AAC: the recorder's own rule is one lossless copy
        # for Whisper (call_recorder AUDIO_* notes). 8 kHz mono is ~1 MB/min.
        "compressWav": False,
        "minDuration": float(min_duration),
        "maxDuration": MAX_DURATION_SECONDS,
        "hideEncrypted": False,
    }
    source: Dict[str, Any] = {
        "driver": "osmosdr",
        # By serial, like OP25 (sdr_settings.pin_op25_device): "rtl=0" means
        # whichever dongle enumerated first, which a decoder can take.
        "device": f"rtl={device_serial}" if device_serial else "rtl=0",
        "rate": int(sample_rate),
        "gain": float(gain),
        "ppm": float(ppm or 0.0),
    }

    if mode == DMR:
        raise ValueError("DMR isn't supported by the edge node.")

    if mode == TRUNKED:
        ccs = [to_hz(c) for c in config.get("control_channels") or []]
        if not ccs:
            raise ValueError(f"System {name!r} has no control channels.")
        talkgroups = [tg for tg in config.get("talkgroups") or [] if tg.get("id") is not None]
        system.update({
            "type": "p25",
            "control_channels": ccs,
            # TR's default; "fsk4" is accepted too. Overridable from C2.
            "modulation": str(config.get("modulation") or "qpsk"),
        })
        # Same rule as OP25's whitelist: a talkgroup list means record only
        # those; no list means record everything.
        if talkgroups:
            files[TALKGROUPS_FILE] = talkgroups_csv(talkgroups)
            system["talkgroupsFile"] = f"{TR_APP_DIR}/{TALKGROUPS_FILE}"
            system["recordUnknown"] = False
        else:
            system["recordUnknown"] = True
        # Voice channels are wherever the system grants them; only the control
        # channels are known here, so the SDR is centred on those. A system
        # whose voice channels sit outside that ~2 MHz loses those calls (TR
        # logs "no source covering" for each) — needs a second source.
        if center_override:
            half = int(sample_rate) * USABLE_BANDWIDTH_SHARE / 2
            covered = [c for c in ccs if abs(c - center_override) <= half]
            if not covered:
                raise ValueError(f"TR_CENTER_HZ {center_override} covers none of {name!r}'s control channels.")
        else:
            covered = widest_cover(ccs, int(sample_rate))
        if len(covered) < len(ccs):
            system["control_channels"] = covered
            logger.warning(
                f"Control channels span {(max(ccs) - min(ccs)) / 1e6:.3f} MHz, wider than one SDR; "
                f"using the {len(covered)} of {len(ccs)} that fit "
                f"({min(covered) / 1e6:.5f}-{max(covered) / 1e6:.5f} MHz)."
            )
        freqs = covered
        source["digitalRecorders"] = int(digital_recorders)
    elif mode == CONVENTIONAL:
        chans = conventional_channels(config)
        if not chans:
            raise ValueError(f"System {name!r} has no conventional channels.")
        # No NAC filter: TR's conventionalP25 records whatever it decodes, so a
        # mistyped NAC can no longer silently drop every call (node-26#33).
        system.update({"type": "conventionalP25", "channelFile": f"{TR_APP_DIR}/{CHANNELS_FILE}",
                       "squelch": float(squelch_db)})
        files[CHANNELS_FILE] = channels_csv(chans)
        freqs = [c["frequency"] for c in chans]
    else:  # ANALOG
        chans = _analog_channels(config)
        if not chans:
            raise ValueError(f"System {name!r} has no analog frequency.")
        squelch = _analog_squelch(config, squelch_db)
        system.update({"type": "conventional", "channelFile": f"{TR_APP_DIR}/{CHANNELS_FILE}",
                       "squelch": squelch})
        files[CHANNELS_FILE] = channels_csv(chans)
        freqs = [c["frequency"] for c in chans]

    source["center"] = choose_center(freqs, int(sample_rate), center_override)

    tr = {
        "ver": 2,
        "sources": [source],
        "systems": [system],
        "tempDir": TR_TEMP_DIR,
        "captureDir": TR_CAPTURE_DIR,
        "callTimeout": CALL_TIMEOUT_SECONDS,
        # File log too (stdout still goes to docker logs): trunk_recorder.py
        # reads the control-channel decode rate from it for the health check.
        "logFile": True,
        "logDir": f"{TR_APP_DIR}/logs",
        "logColor": "none",  # TR 5.x: a string ("console"|"none"), not a bool
        # Live audio for Discord: simplestream sends every call's PCM to the
        # edge-node, which follows one talkgroup at a time into PulseAudio.
        "audioStreaming": True,
        "plugins": [{
            "name": "simplestream",
            "library": "libsimplestream.so",
            "streams": [{"TGID": 0, "address": "127.0.0.1", "port": int(live_port), "sendTGID": True}],
        }],
    }
    return tr, files


def live_sample_rate(system_type: str, config: Dict[str, Any]) -> int:
    """simplestream sends analog audio at 16 kHz and digital at 8 kHz (Plugins.md)."""
    return 16000 if system_mode(system_type, config or {}) == ANALOG else 8000
