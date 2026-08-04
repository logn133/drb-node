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
    call_idle_timeout: float = 3.0

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
