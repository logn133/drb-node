"""
What kind of radio system a SystemConfig describes, and the op25-container
generate-config payload for it.

C2 system documents (the `config` blob is passed through untouched):

    P25 trunked       type "P25", config.mode absent or "trunked",
                      config.control_channels = [MHz, ...]
    P25 conventional  type "P25", config.mode "conventional",
                      config.channels = [{freq: MHz, name, nac?}, ...]   (node-26#24)
    Analog            type "NBFM", config.channels = [{freq, name}] — or, from
                      before #24, config.control_channels[0]

A missing mode is trunked, so every system saved before #24 behaves exactly as
it did. Pure functions; main._generate_op25_config does the I/O.
"""
from typing import Any, Dict, List, Optional

TRUNKED, CONVENTIONAL, ANALOG, DMR = "trunked", "conventional", "analog", "dmr"

# Mirrors op25-container's MAX_CONVENTIONAL_CHANNELS: one audio.py carries two.
MAX_CONVENTIONAL_CHANNELS = 2


def to_hz(freq: Any) -> int:
    """MHz floats (< 1e6) or Hz ints -> Hz."""
    f = float(freq)
    return int(round(f * 1_000_000)) if f < 1_000_000 else int(f)


def parse_nac(value: Any) -> Optional[int]:
    """NAC as typed by an admin ("293", "0x293") or stored as an int. Hex, like
    RadioReference prints it. None when blank; ValueError when nonsense."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError(f"NAC {value!r} isn't a number.")
    if isinstance(value, int):
        nac = value
    else:
        text = str(value).strip().lower()
        text = text[2:] if text.startswith("0x") else text
        try:
            nac = int(text, 16)
        except ValueError:
            raise ValueError(f"NAC {value!r} isn't hex (e.g. 293 or 0x293).")
    if not 0 < nac <= 0xFFF:
        raise ValueError(f"NAC {value!r} is outside 001-FFF.")
    return nac


def system_mode(system_type: str, config: Dict[str, Any]) -> str:
    t = str(system_type or "").upper()
    if t in ("NBFM", "ANALOG"):
        return ANALOG
    if t == "DMR":
        return DMR
    mode = str((config or {}).get("mode") or TRUNKED).lower()
    return CONVENTIONAL if mode == CONVENTIONAL else TRUNKED


def system_kind(system_type: str, config: Dict[str, Any]) -> str:
    """Label for dashboards: "P25 trunked", "P25 conventional", "Analog FM", "DMR"."""
    return {
        TRUNKED: "P25 trunked", CONVENTIONAL: "P25 conventional", ANALOG: "Analog FM", DMR: "DMR",
    }[system_mode(system_type, config)]


def conventional_channels(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """config.channels -> [{frequency: Hz, name, nac}] (ValueError if malformed)."""
    out = []
    for raw in (config or {}).get("channels") or []:
        if not isinstance(raw, dict) or raw.get("freq") in (None, ""):
            raise ValueError("Every conventional channel needs a frequency.")
        out.append({
            "frequency": to_hz(raw["freq"]),
            "name": str(raw.get("name") or "").strip(),
            "nac": parse_nac(raw.get("nac")),
        })
    return out


def analog_frequency(config: Dict[str, Any]) -> Optional[int]:
    chans = (config or {}).get("channels") or []
    if chans and isinstance(chans[0], dict) and chans[0].get("freq") not in (None, ""):
        return to_hz(chans[0]["freq"])
    legacy = (config or {}).get("control_channels") or []
    return to_hz(legacy[0]) if legacy else None


def build_generate_payload(
    system_type: str,
    name: str,
    config: Dict[str, Any],
    icecast: Dict[str, Any],
    hardware_preset: str,
    ppm_override: Optional[float],
) -> Dict[str, Any]:
    """SystemConfig fields -> op25-container /generate-config body."""
    mode = system_mode(system_type, config)
    payload: Dict[str, Any] = {
        "type": system_type,
        "systemName": name,
        "icecastConfig": icecast,
        "hardware_preset": hardware_preset,
    }
    if ppm_override is not None:
        payload["ppm_override"] = ppm_override

    if mode == CONVENTIONAL:
        payload["type"] = "P25"
        payload["p25_mode"] = CONVENTIONAL
        payload["channels"] = []
        payload["conv_channels"] = conventional_channels(config)
        return payload

    if mode == ANALOG:
        freq = analog_frequency(config)
        payload["channels"] = [freq] if freq else []
        return payload

    talkgroups = (config or {}).get("talkgroups", []) or []
    payload["channels"] = [to_hz(ch) for ch in (config or {}).get("control_channels", []) or []]
    payload["tags"] = [
        {"talkgroup": str(tg.get("name", "")), "tagDec": int(tg["id"])}
        for tg in talkgroups if tg.get("id") is not None
    ]
    payload["whitelist"] = [int(tg["id"]) for tg in talkgroups if tg.get("id") is not None]
    return payload
