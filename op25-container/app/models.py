from pydantic import BaseModel
from typing import List, Optional, Union
from enum import Enum
from config import bind_host

# Preset device settings for common RTL-SDR hardware.
# gains: OP25 gain string passed to the device block.
# ppm: frequency offset correction (user should calibrate and override).
# cqpsk_tracking: disable for TCXO dongles (RTL-SDR v3, NESDR) once PPM is dialled in;
#                 leave enabled for unknown hardware as a safety net.
HARDWARE_PRESETS: dict = {
    "rtl-sdr-v3": {
        "gains": "lna:34",
        "ppm": 0.0,
        "cqpsk_tracking": False,
    },
    "nesdr-smart-v4": {
        "gains": "lna:32",
        "ppm": 0.0,
        "cqpsk_tracking": False,
    },
    "other": {
        "gains": "lna:32",
        "ppm": 0.0,
        "cqpsk_tracking": True,
    },
}

class DecodeMode(str, Enum):
    P25 = "P25"
    DMR = "DMR"
    ANALOG = "NBFM"

class TalkgroupTag(BaseModel):
    talkgroup: str
    tagDec: int

# Defined before ConfigGenerator, which annotates a field with it. Under
# Python 3.14 (PEP 649) annotations are evaluated lazily, so the original
# order happened to work in the container; on 3.13 or earlier it is a hard
# NameError at import. Keep the definition above its first use so this file
# does not depend on the base image's Python version.
class IcecastConfig(BaseModel):
    icecast_host: str
    icecast_port: int
    icecast_mountpoint: str
    icecast_password: str
    icecast_description: Optional[str] = "OP25"
    icecast_genre: Optional[str] = "Public Safety"


class ConfigGenerator(BaseModel):
    type: DecodeMode
    systemName: str
    channels: List[Union[int, str]]
    tags: Optional[List[TalkgroupTag]]
    whitelist: Optional[List[int]]
    icecastConfig: Optional[IcecastConfig]
    hardware_preset: str = "rtl-sdr-v3"
    ppm_override: Optional[float] = None  # when set, overrides the preset's default PPM

class DemodType(str, Enum):
    CQPSK = "cqpsk"
    FSK4 = "fsk4"

class FilterType(str, Enum):
    RC = "rc"
    WIDEPULSE = "widepulse"

class ChannelConfig(BaseModel):
    name: str
    trunking_sysname: Optional[str]
    enable_analog: str
    meta_stream_name: str
    demod_type: DemodType
    filter_type: FilterType
    device: Optional[str] = "sdr"
    cqpsk_tracking: Optional[bool] = None
    frequency: Optional[float] = None
    nbfmSquelch: Optional[float] = None
    destination: Optional[str] = "udp://127.0.0.1:23456"
    tracking_threshold: Optional[int] = 120
    tracking_feedback: Optional[float] = 0.75
    excess_bw: Optional[float] = 0.2
    if_rate: Optional[int] = 24000
    plot: Optional[str] = ""
    symbol_rate: Optional[int] = 4800
    blacklist: Optional[str] = ""
    whitelist: Optional[str] = ""

class DeviceConfig(BaseModel):
    args: Optional[str] = "rtl"
    gains: Optional[str] = "lna:39"
    gain_mode: Optional[bool] = False
    name: Optional[str] = "sdr"
    offset: Optional[int] = 0
    ppm: Optional[float] = 0.0
    rate: Optional[int] = 1920000
    usable_bw_pct: Optional[float] = 0.85
    tunable: Optional[bool] = True

class TrunkingChannelConfig(BaseModel):
    sysname: str
    control_channel_list: str
    tagsFile: Optional[str] = None
    whitelist: Optional[str] = None
    nac: Optional[str] = ""
    wacn: Optional[str] = ""
    tdma_cc: Optional[bool] = False
    crypt_behavior: Optional[int] = 2

class TrunkingConfig(BaseModel):
    module: str
    chans: List[TrunkingChannelConfig]

class MetadataStreamConfig(BaseModel):
    stream_name: str = "stream_0"
    meta_format_idle: str = "[idle]"
    meta_format_tgid: str = "[%TGID%]"
    meta_format_tag: str = "[%TGID%] %TAG%"
    icecastServerAddress: str = "localhost"
    icecastMountpoint: str = "NODE_ID"
    icecastMountExt: str = ".xspf"
    icecastPass: str = "PASSWORD"
    delay: float = 0.0

class MetadataConfig(BaseModel):
    module: str = "icemeta.py"
    streams: List[MetadataStreamConfig]

class TerminalConfig(BaseModel):
    module: Optional[str] = "terminal.py"
    # Bind address comes from OP25_DEBUG_EXPOSE (config.py) — 127.0.0.1 unless
    # that flag is set. See config.py for why.
    terminal_type: Optional[str] = f"http:{bind_host()}:8081"
    terminal_timeout: Optional[float] = 5.0
    curses_plot_interval: Optional[float] = 0.2
    http_plot_interval: Optional[float] = 1.0
    http_plot_directory: Optional[str] = "../www/images"
    tuning_step_large: Optional[int] = 1200
    tuning_step_small: Optional[int] = 100



### ======================================================
# Icecast models
# (IcecastConfig itself is defined above ConfigGenerator, which references it.)