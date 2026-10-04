"""
Attribution for CONVENTIONAL channels — P25 conventional (node-26#24) and
analog NBFM (node-26#20) share this one path.

metadata_watcher labels audio with whatever the OP25 console says was talking.
On a trunked system that is a talkgroup from a control-channel grant. On a
conventional channel there is no control channel, so:

  * P25 conventional: link control in each LDU1 usually carries the talkgroup
    and unit id, and our tk_drb_conv module adds `voice` (frames arriving) and
    encryption. The talkgroup is often the P25 default (1) or "everyone"
    (0xFFFF) and identical on every channel, which says nothing about WHICH
    channel was keyed, and sometimes it is missing altogether.
  * Analog: OP25 reports the channel with tgid None and nothing else — the
    console never says anyone is talking.

So a conventional channel gets a stable SYNTHETIC talkgroup id derived from its
frequency (channel_tgid), used whenever link control gives nothing that
distinguishes the channel. A real, distinct link-control talkgroup is still
used when present. normalize() rewrites the console update so the rest of the
watcher sees ordinary "talkgroup X is active" rows and grants and needs no
conventional special cases, except one: with exactly ONE conventional channel
configured, every sound OP25 puts on drb_sink came from that channel, so audio
the console said nothing about is attributed to it (sole_channel) instead of
being discarded as orphan audio. With two channels there is no such certainty
and orphan audio is still discarded (DEFERRED.md, metadata_watcher row).

Encrypted conventional traffic is never a talker: the decoder mutes it, so no
audio follows, and letting it compete for attribution could mislabel a clear
call on the other channel. It is reported instead (encryption_report).
"""
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

# P25 talkgroups that identify no particular group: TIA-102 default (1) and
# "everyone" (0xFFFF). 0 is "none" and already rejected by _as_int.
NON_DISTINCT_TGIDS = frozenset({1, 0xFFFF})

# Synthetic ids are frequency in 100 Hz units: 460.400 MHz -> 4_604_000. Any
# RTL-SDR frequency (>= 24 MHz) gives >= 240_000, clear of the 16-bit P25
# talkgroup space, and 6.25 kHz channel steps stay distinct. C2 resolves the
# same number back to the channel name (server talkgroups.name_from_system).
SYNTHETIC_TGID_MIN = 240_000

# How long a channel's last real talkgroup is remembered for a transmission
# whose link control hasn't decoded (yet). Covers the back-and-forth of one
# conversation; past it, an undecoded call is labelled with the channel.
REMEMBER_TGID_SECONDS = 30.0


def channel_tgid(freq: Any) -> Optional[int]:
    """Stable synthetic talkgroup id for a conventional channel frequency (Hz or MHz)."""
    try:
        f = float(freq)
    except (TypeError, ValueError):
        return None
    if f <= 0:
        return None
    hz = f * 1_000_000 if f < 1_000_000 else f
    return int(round(hz / 100))


def _as_int(value: Any) -> Optional[int]:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _truthy(value: Any) -> bool:
    return bool(value) and value not in ("0", "false", "False")


ALGID_NAMES = {
    0x81: "DES-OFB", 0x83: "Triple DES", 0x84: "AES-256", 0x85: "AES-128",
    0x9F: "DES-XL", 0xA0: "DVI-XL", 0xA1: "DVP-XL", 0xAA: "ADP (RC4)",
}


def algid_name(algid: Any) -> str:
    a = _as_int(algid)
    if a is None:
        return "unknown algorithm"
    return f"{ALGID_NAMES.get(a, 'algorithm')} (ALGID 0x{a:02X})"


class ConventionalChannels:
    """Per-poll normalizer plus what it has learned about the channels."""

    def __init__(self) -> None:
        # msgqid -> latest raw conventional row. channel_update is not on every
        # poll, so this outlives a single update.
        self._rows: Dict[str, Dict[str, Any]] = {}
        self._known = False   # have we seen a channel_update at all
        # synthetic channel id -> the talkgroup that channel last carried. A
        # transmission's header frame sets `voice` ~180 ms before its first
        # link control names the talkgroup; without this, every such poll
        # would label the call with the synthetic id and the real talkgroup
        # a poll later would force a bogus split (drb-audio-review, #24).
        self._last_identity: Dict[int, Tuple[int, float]] = {}
        self._clock: Callable[[], float] = time.time

    # -- identity --------------------------------------------------------

    @staticmethod
    def _name(row: Dict[str, Any]) -> str:
        name = str(row.get("name") or "").strip()
        if name and not name.startswith("["):
            return name
        f = row.get("freq")
        try:
            return f"{float(f) / 1e6:.4f} MHz"
        except (TypeError, ValueError):
            return "Conventional"

    def identity(self, row: Dict[str, Any], lc_tgid: Optional[int]) -> Optional[int]:
        """Distinctive link-control talkgroup; else (no link control decoded
        yet) what this channel last carried; else the channel's synthetic id."""
        synthetic = channel_tgid(row.get("freq"))
        now = self._clock()
        if lc_tgid is not None and lc_tgid not in NON_DISTINCT_TGIDS:
            if synthetic is not None:
                self._last_identity[synthetic] = (lc_tgid, now)
            return lc_tgid
        remembered = self._last_identity.get(synthetic) if synthetic is not None else None
        if lc_tgid is None and remembered and now - remembered[1] <= REMEMBER_TGID_SECONDS:
            self._last_identity[synthetic] = (remembered[0], now)   # still the same conversation
            return remembered[0]
        if synthetic is not None and lc_tgid is not None:
            self._last_identity[synthetic] = (synthetic, now)   # explicit default TG: channel identity
        return synthetic

    # -- normalization ---------------------------------------------------

    def normalize(self, update: Any) -> Any:
        """Rewrite conventional rows and grants in place; trunked ones untouched."""
        if update.channels:
            self._known = True
            self._rows = {}
        for i, row in enumerate(update.channels):
            if not _truthy(row.get("conventional")):
                continue
            key = str(row.get("msgqid", i))
            self._rows[key] = row
            update.channels[i] = self._normalize_row(row)

        for i, entry in enumerate(update.call_log):
            row = self._rows.get(str(entry.get("rcvr")))
            if row is None:
                continue
            lc = _as_int(entry.get("tgid"))
            tgid = self.identity(row, lc)
            fixed = dict(entry)
            fixed["lc_tgid"] = lc
            fixed["tgid"] = tgid
            fixed["tgtag"] = self._label(row, lc, entry.get("tgtag"))
            fixed["freq"] = entry.get("freq") or row.get("freq")
            update.call_log[i] = fixed
        return update

    def _label(self, row: Dict[str, Any], lc: Optional[int], tag: Any) -> str:
        name = self._name(row)
        tag = str(tag or "").strip()
        if lc is not None and lc not in NON_DISTINCT_TGIDS and tag and tag not in ("Conventional", name):
            return tag
        return name

    def _normalize_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        lc = _as_int(row.get("tgid"))
        encrypted = _truthy(row.get("encrypted"))
        # tgid is only ever set while a call is up (tk_p25 / tk_drb_conv clear
        # it on TDU and sync timeout); `voice` is tk_drb_conv's own signal.
        active = (lc is not None or _truthy(row.get("voice"))) and not encrypted
        out = dict(row)
        out["lc_tgid"] = lc
        out["tgid"] = self.identity(row, lc) if active else None
        out["tag"] = self._label(row, lc, row.get("tag"))
        out["drb_conventional"] = True
        out["drb_active"] = active
        return out

    # -- what the watcher asks -------------------------------------------

    def same_channel_relabel(self, current_tgid: Optional[int], current_freq: Any,
                             new_tgid: Optional[int], new_freq: Any) -> Optional[str]:
        """Two labels for ONE conventional channel are not a talkgroup change.

        "adopt": the open segment holds the channel's synthetic id and the
                 console now names the real talkgroup — relabel, don't split.
        "keep":  the segment holds a real talkgroup and the console offers the
                 synthetic id (link control not decoded on this frame) — ignore.
        None:    not the same conventional channel; the normal split rules apply.
        """
        if current_tgid is None or new_tgid is None or current_tgid == new_tgid:
            return None
        synthetic = channel_tgid(new_freq)
        if synthetic is None or channel_tgid(current_freq) != synthetic:
            return None
        if not any(channel_tgid(r.get("freq")) == synthetic for r in self._rows.values()):
            return None   # trunked: freq says nothing about identity
        if current_tgid == synthetic:
            return "adopt"
        if new_tgid == synthetic:
            return "keep"
        return None

    def sole_channel(self) -> Optional[Dict[str, Any]]:
        """The one conventional channel this node listens to, if exactly one
        (and it is not encrypted). None on any trunked or multi-channel setup."""
        if len(self._rows) != 1:
            return None
        row = next(iter(self._rows.values()))
        tgid = channel_tgid(row.get("freq"))
        if tgid is None:
            return None
        if _truthy(row.get("encrypted")):
            return None
        return {"tgid": tgid, "name": self._name(row), "freq": row.get("freq")}

    def encryption_report(self) -> List[Dict[str, Any]]:
        """Per conventional channel: is it (or was its last call) encrypted."""
        out = []
        for row in self._rows.values():
            enc_calls = _as_int(row.get("enc_calls")) or 0
            clear_calls = _as_int(row.get("clear_calls")) or 0
            now_enc = _truthy(row.get("encrypted"))
            last_enc = row.get("last_enc_time") or 0
            last_clear = row.get("last_clear_time") or 0
            out.append({
                "name": self._name(row),
                "freq": row.get("freq"),
                "nac": _as_int(row.get("nac")),
                "encrypted_now": now_enc,
                "enc_calls": enc_calls,
                "clear_calls": clear_calls,
                # The channel "is encrypted" when its most recent call was.
                "encrypted": now_enc or (enc_calls > 0 and float(last_enc) >= float(last_clear)),
                "algid": _as_int(row.get("algid")) or _as_int(row.get("last_enc_algid")),
                "keyid": _as_int(row.get("keyid")) or _as_int(row.get("last_enc_keyid")),
                "voice": _truthy(row.get("voice")),
            })
        return out
