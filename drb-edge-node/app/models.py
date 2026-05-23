from pydantic import BaseModel
from typing import Optional, Dict, Any
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
