"""
Trunk Recorder call JSON -> what DRB sends for one call (node-26#45).

TR writes one JSON per call next to its WAV and passes both to uploadScript.
The shape is `create_call_json` in trunk-recorder/call_concluder/
call_concluder.cc at v5.2.1 (keys, in order: freq, freq_error, signal, noise,
source_num, recorder_num, tdma_slot, phase2_tdma, start_time, stop_time,
start_time_ms, stop_time_ms, emergency, priority, mode, duplex, encrypted,
call_length, call_length_ms, talkgroup, talkgroup_tag, talkgroup_description,
talkgroup_group_tag, talkgroup_group, color_code, audio_type, short_name,
patched_talkgroups?, freqList[], srcList[]). Older TR releases omit the *_ms
keys, so both are read.

Pure: trunk_recorder.py does the I/O.
"""
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

DRIVER = "trunk-recorder"
# Fixed namespace so a call's id is a pure function of its JSON: re-reading the
# same file after a crash yields the same call_id, so C2 overwrites rather than
# duplicates (trunk_recorder.py relies on this to be restart-safe).
CALL_ID_NAMESPACE = uuid.UUID("5d0c7f4e-2b8f-4e0e-9a59-45c0de7c0045")


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _epoch(data: Dict[str, Any], key: str) -> Optional[float]:
    ms = data.get(f"{key}_ms")
    if isinstance(ms, (int, float)) and ms > 0:
        return ms / 1000.0
    s = data.get(key)
    if isinstance(s, (int, float)) and s > 0:
        return float(s)
    return None


@dataclass
class TrCall:
    call_id: str
    tgid: int
    tgid_name: str
    freq: Optional[int]
    start_epoch: float
    stop_epoch: float
    units: List[int] = field(default_factory=list)
    emergency: bool = False
    encrypted: bool = False
    src_list: List[Dict[str, Any]] = field(default_factory=list)
    freq_list: List[Dict[str, Any]] = field(default_factory=list)
    call_length: Optional[float] = None
    signal: Optional[int] = None
    noise: Optional[int] = None
    freq_error: Optional[int] = None
    decode_errors: Optional[int] = None
    spikes: Optional[int] = None
    patched_talkgroups: List[int] = field(default_factory=list)
    short_name: str = ""

    @property
    def srcaddr(self) -> Optional[int]:
        return self.units[0] if self.units else None

    def start_payload(self) -> Dict[str, Any]:
        """MQTT call_start — the keys main.on_call_start publishes, which C2's
        _on_call_start reads (tgid, tgid_name, freq, srcaddr, started_at)."""
        return {
            "call_id": self.call_id,
            "tgid": self.tgid,
            "tgid_name": self.tgid_name,
            "freq": self.freq,
            "srcaddr": self.srcaddr,
            "started_at": _iso(self.start_epoch),
            "started_at_epoch": self.start_epoch,
            "attributed": True,
            "driver": DRIVER,
        }

    def audio_end_epoch(self, wav_seconds: Optional[float] = None) -> float:
        """Wall clock at the END of the audio file.

        NOT stop_time: TR joins a call's transmissions back to back and drops
        the gaps between them, so the WAV is shorter than stop - start by the
        sum of the gaps. start + WAV length is what the file's last sample
        maps to only if there were no gaps; the exact per-transmission map is
        srcList/freqList (time = wall clock, pos = offset in the file), sent
        as src_list / freq_list.
        """
        if wav_seconds is None:
            return self.stop_epoch
        return self.start_epoch + wav_seconds

    def end_payload(self, wav_seconds: Optional[float] = None) -> Dict[str, Any]:
        """MQTT call_end. TR cut the call on decoder boundaries with no
        trimming, so the audio starts at the call start; see audio_end_epoch
        for why it does not end at the call end."""
        audio_end = self.audio_end_epoch(wav_seconds)
        out = self.start_payload()
        out.update({
            "ended_at": _iso(self.stop_epoch),
            "ended_at_epoch": self.stop_epoch,
            "audio_start_at": _iso(self.start_epoch),
            "audio_end_at": _iso(audio_end),
            "audio_start_epoch": self.start_epoch,
            "audio_end_epoch": audio_end,
            "units": self.units,
            "emergency": self.emergency,
            "encrypted": self.encrypted,
        })
        return out

    def upload_extra(self) -> Dict[str, str]:
        """Extra /upload form fields. C2 declares none of these yet, so FastAPI
        ignores them (same as audio_start_epoch today); they are here for when
        it does."""
        extra = {
            "recorder_backend": DRIVER,
            "units": ",".join(str(u) for u in self.units),
            "src_list": json.dumps(self.src_list, separators=(",", ":")),
            "freq_list": json.dumps(self.freq_list, separators=(",", ":")),
            "emergency": "1" if self.emergency else "0",
            "encrypted": "1" if self.encrypted else "0",
            "started_at_epoch": f"{self.start_epoch:.3f}",
            "ended_at_epoch": f"{self.stop_epoch:.3f}",
        }
        optional = {
            "freq": self.freq, "call_length": self.call_length, "signal": self.signal, "noise": self.noise,
            "freq_error": self.freq_error, "decode_errors": self.decode_errors, "spikes": self.spikes,
        }
        extra.update({k: str(v) for k, v in optional.items() if v is not None})
        if self.patched_talkgroups:
            extra["patched_talkgroups"] = ",".join(str(t) for t in self.patched_talkgroups)
        return extra


def parse_call(data: Dict[str, Any], node_id: str, file_stem: str = "") -> TrCall:
    """One TR call JSON -> TrCall. ValueError if it has no talkgroup or start."""
    tgid = _int(data.get("talkgroup"))
    if not tgid or tgid <= 0:
        raise ValueError(f"call JSON has no talkgroup ({data.get('talkgroup')!r})")
    start = _epoch(data, "start_time")
    if start is None:
        raise ValueError("call JSON has no start_time")
    stop = _epoch(data, "stop_time")
    length = data.get("call_length_ms")
    length_s = (length / 1000.0) if isinstance(length, (int, float)) and length > 0 else None
    if length_s is None and isinstance(data.get("call_length"), (int, float)):
        length_s = float(data["call_length"])
    if stop is None or stop < start:
        stop = start + (length_s or 0.0)

    src_list: List[Dict[str, Any]] = []
    units: List[int] = []
    unit_emergency = False
    for s in data.get("srcList") or []:
        if not isinstance(s, dict):
            continue
        src = _int(s.get("src"))
        entry = {"src": src, "time": s.get("time"), "pos": s.get("pos"),
                 "emergency": bool(_int(s.get("emergency")))}
        if s.get("tag"):
            entry["tag"] = s.get("tag")
        src_list.append(entry)
        unit_emergency = unit_emergency or entry["emergency"]
        # TR reports 0 / -1 when the unit id wasn't decoded.
        if src and src > 0 and src not in units:
            units.append(src)

    freq_list = [f for f in data.get("freqList") or [] if isinstance(f, dict)]
    errors = sum(_int(f.get("error_count")) or 0 for f in freq_list) if freq_list else None
    spikes = sum(_int(f.get("spike_count")) or 0 for f in freq_list) if freq_list else None

    freq = _int(data.get("freq"))
    short_name = str(data.get("short_name") or "")
    key = f"{node_id}|{short_name}|{tgid}|{start:.3f}|{freq}|{file_stem}"
    return TrCall(
        call_id=str(uuid.uuid5(CALL_ID_NAMESPACE, key)),
        tgid=tgid,
        tgid_name=str(data.get("talkgroup_tag") or data.get("talkgroup_description") or ""),
        freq=freq,
        start_epoch=start,
        stop_epoch=stop,
        units=units,
        emergency=bool(_int(data.get("emergency"))) or unit_emergency,
        encrypted=bool(_int(data.get("encrypted"))),
        src_list=src_list,
        freq_list=[{"freq": _int(f.get("freq")), "time": f.get("time"), "pos": f.get("pos"), "len": f.get("len"),
                    "error_count": _int(f.get("error_count")), "spike_count": _int(f.get("spike_count"))}
                   for f in freq_list],
        call_length=round(length_s, 3) if length_s is not None else None,
        signal=_int(data.get("signal")),
        noise=_int(data.get("noise")),
        freq_error=_int(data.get("freq_error")),
        decode_errors=errors,
        spikes=spikes,
        patched_talkgroups=[t for t in (_int(x) for x in data.get("patched_talkgroups") or []) if t],
        short_name=short_name,
    )
