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
    # transmission — which is usually the disposition or the address.
    #
    # Raised 1.0 -> 3.0 after field measurement showed the recording WINDOW
    # (anchored to OP25 control-channel timestamps) closing well before the
    # actual voice audio arrives: grant->speech offset measured 0.84-1.62s
    # across 7 calls (~1.5s typical). At the old 1.0s pad, a short
    # transmission (e.g. a 0.97s control-channel call) had its window close
    # at T+1.97 while voice didn't start until ~T+1.5 — leaving ~0.4s of
    # captured speech, clipped mid-word. Confirmed by a 0.57s output file
    # whose final 0.10s measured -12.2dB, louder than its own -18.2dB
    # average (i.e. clipped speech, not trailing silence), and by two short
    # calls that produced no "Trimmed" log line at all because there was no
    # trailing silence left to trim.
    #
    # Safe to be generous here: trim_silence already strips trailing silence
    # back to trim_silence_guard_seconds before upload, so a larger pad costs
    # long calls nothing (the extra is trimmed away) while giving short
    # transmissions enough window to actually capture the voice. Over-capture
    # is free; under-capture loses words permanently. Do not tune this back
    # down without new field data showing the grant->speech offset has
    # shrunk — see DEFERRED.md for the call_idle_timeout coupling this value
    # now sits at.
    call_tail_pad_seconds: float = 3.0

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
