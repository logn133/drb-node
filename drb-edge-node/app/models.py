from pydantic import BaseModel, model_validator
from typing import Optional, Dict, Any, Iterable, List
from enum import Enum
from datetime import datetime


class NodeStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    RECORDING = "recording"
    UNCONFIGURED = "unconfigured"


class CallStatus(str, Enum):
    ACTIVE = "active"
    ENDED = "ended"


class SystemConfig(BaseModel):
    system_id: str
    name: str
    type: str  # P25, DMR, NBFM
    config: Dict[str, Any]  # OP25-compatible config blob passed through to op25-container


# Decoders a node can run on the SDRs beyond op25's (node-26#9). op25 always
# keeps its own dongle; each further dongle runs the next entry of the node's
# secondary_sdr_priority, so a 3-SDR node with ["adsb", "ais"] runs both.
SECONDARY_SDR_MODES = ("adsb", "ais")


def normalize_secondary_priority(items: Iterable[str]) -> List[str]:
    """Known modes only, first occurrence wins, order preserved."""
    out: List[str] = []
    for m in items or []:
        if m in SECONDARY_SDR_MODES and m not in out:
            out.append(m)
    return out


# Services that can be bound to a specific dongle by its USB serial.
SDR_PIN_KEYS = ("op25",) + SECONDARY_SDR_MODES


def normalize_sdr_pins(pins: Dict[str, Optional[str]]) -> Dict[str, str]:
    """Known services only, blank = automatic (dropped). Two services can't
    share a dongle, so a serial claimed twice raises."""
    out = {k: str(v).strip() for k, v in (pins or {}).items() if k in SDR_PIN_KEYS and v and str(v).strip()}
    if len(set(out.values())) != len(out):
        raise ValueError("Two services can't be pinned to the same SDR.")
    return out


class NodeConfig(BaseModel):
    node_id: str
    node_name: str
    lat: float
    lon: float
    assigned_system_id: Optional[str] = None
    system_config: Optional[SystemConfig] = None
    configured: bool = False
    hardware_preset: str = "rtl-sdr-v3"
    ppm_override: Optional[float] = None
    node_type: str = "fixed"  # fixed or portable
    secondary_sdr_priority: List[str] = []  # ordered; SDRs beyond op25's run these top-down
    sdr_pins: Dict[str, str] = {}  # service (op25/adsb/ais) -> dongle serial; absent = automatic
    # Legacy single-mode field (pre-priority). Still written as priority[0] so
    # an older C2 reading checkins sees something sensible; read only to
    # migrate a node_config.json saved before priority existed.
    secondary_sdr_mode: str = "none"
    enforce_override_timeout: bool = True
    override_system_id: Optional[str] = None
    override_config: Optional[SystemConfig] = None
    offline_call_buffer_size: int = 35  # max call_end events to buffer while MQTT is offline

    @model_validator(mode="after")
    def _migrate_secondary_mode(self) -> "NodeConfig":
        if not self.secondary_sdr_priority and self.secondary_sdr_mode in SECONDARY_SDR_MODES:
            self.secondary_sdr_priority = [self.secondary_sdr_mode]
        return self


class CallEvent(BaseModel):
    call_id: str
    node_id: str
    system_id: Optional[str] = None
    talkgroup_id: Optional[int] = None
    talkgroup_name: Optional[str] = None
    started_at: datetime
    ended_at: Optional[datetime] = None
    audio_url: Optional[str] = None
    status: CallStatus = CallStatus.ACTIVE


class DiscordCommand(BaseModel):
    action: str  # "join" or "leave"
    guild_id: Optional[str] = None
    channel_id: Optional[str] = None
