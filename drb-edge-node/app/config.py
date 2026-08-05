from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    # Node identity
    node_id: str
    node_name: str = "Unnamed Node"
    node_lat: float = 0.0
    node_lon: float = 0.0

    # MQTT
    mqtt_broker: str
    mqtt_port: int = 1883
    mqtt_user: Optional[str] = None
    mqtt_pass: Optional[str] = None

    # C2 server (audio upload destination); None disables upload
    c2_url: Optional[str] = None

    # Local Icecast — live listening only (frontend / mobile).
    # NOT used for call recording or Discord voice: it lags 1s and drifts to 100s+.
    icecast_host: str = "localhost"
    icecast_port: int = 8000
    icecast_mount: str = "/radio"
    icecast_source_password: str = "hackme"

    # PulseAudio — the low-latency path used for call recording and Discord voice.
    # Liquidsoap (op25 container) writes into the `drb_sink` null sink; we capture
    # its monitor. Addressed explicitly rather than via "default" because the op25
    # entrypoint starts pulseaudio with -n and never applies system.pa's
    # `set-default-source` line.
    pulse_source: str = "drb_sink.monitor"
    # Bounded wait for the shared PulseAudio socket before launching FFmpeg.
    pulse_wait_timeout: float = 30.0

    # Call segmentation — seconds with no active transmission before the current
    # recording is closed out. Consecutive grants on the SAME talkgroup inside this
    # window are kept in one recording so back-and-forth traffic stays together.
    #
    # Do NOT tune this against measured *audio* silence: audio gaps also contain
    # the ~1.9 s P25 grant→speech delay, so they are always longer than the
    # control-channel idle this timer measures. metadata_watcher logs the real
    # measured idle on every idle-timeout close — tune from that.
    call_idle_timeout: float = 3.0

    # Audio kept after the observed end of the last transmission. The srcaddr
    # 1→0 edge can be up to one poll (0.5 s) late and the encoder adds its own
    # latency, so this is the only headroom protecting the last word of a
    # transmission — which is usually the disposition or the address. Field
    # measurement at 0.5 s left only 0.29–0.37 s of real trailing margin and one
    # recording ended mid-word, hence 1.0 s.
    call_tail_pad_seconds: float = 1.0

    # Strip leading/trailing dead air before upload. ~63% of a typical recording
    # is silence (the grant→speech delay plus the tail pad), which inflates
    # Whisper cost and is a well-documented trigger for hallucinated transcript
    # text. Trimming is conservative — see internal/audio_trim.py.
    trim_silence: bool = True
    # Anything quieter than this counts as silence for detection purposes.
    trim_silence_threshold_db: float = -40.0
    # Guard margin kept around detected speech so no syllable is clipped.
    trim_silence_guard_seconds: float = 0.25

    # OP25 container
    op25_api_url: str = "http://localhost:8001"
    op25_terminal_url: str = "http://localhost:8081"

    # Paths (volume mounts)
    config_path: str = "/configs"
    recordings_path: str = "/recordings"

    # Offline call buffer — how many call_end events to keep while disconnected
    offline_call_buffer_size: int = 35

    class Config:
        env_file = ".env"


settings = Settings()
