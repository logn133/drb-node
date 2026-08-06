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

    # ------------------------------------------------------------------
    # Call segmentation
    #
    # Boundaries come from the AUDIO, not the control channel. A recording
    # starts at voice onset and ends after call_silence_timeout seconds of
    # silence actually heard in the stream. See internal/metadata_watcher.py
    # for why the control channel is no longer trusted for either edge.
    # ------------------------------------------------------------------

    # Seconds of continuous silence IN THE AUDIO before the current recording is
    # closed. This is the primary segmentation control. Consecutive
    # transmissions on the SAME talkgroup separated by less than this stay in
    # one recording, so back-and-forth traffic is one file.
    #
    # Defaults to 3.0 to match the behaviour of the control-channel idle timer
    # it replaces, but it is NOT the same clock: this one measures real silence
    # in the audio, with no grant->speech delay mixed in. metadata_watcher logs
    # the measured trailing silence on every close — tune from that number.
    call_silence_timeout: float = 3.0

    # dBFS (RMS, measured over one ~46ms capture chunk) below which audio counts
    # as silence for the purpose of ending a recording.
    #
    # This does NOT need field calibration against radio noise. Between
    # transmissions the capture is the monitor of a PulseAudio *null sink*,
    # which emits digital silence, not an analog noise floor: measured on a live
    # node the gap sits at about -91 dBFS, i.e. one least-significant bit of a
    # 16-bit sample. Speech on the same node averages about -18 dBFS. Anything
    # between roughly -70 and -40 therefore behaves identically; -50 is chosen
    # to sit far below even quiet speech while staying far above the floor.
    call_silence_threshold_db: float = -50.0

    # DEPRECATED as a primary control — used ONLY in console fallback mode, i.e.
    # when PulseAudio capture is not producing audio and there is nothing to
    # segment on. Then, and only then, the old control-channel state machine
    # runs and closes a segment this many seconds after the last observed
    # transmission. Those segments carry no audio; they exist so the node keeps
    # reporting real radio activity to C2 while its audio path is broken.
    #
    # Do NOT tune this against measured *audio* silence — use
    # call_silence_timeout for that.
    call_idle_timeout: float = 3.0

    # Audio kept past a CONSOLE-DERIVED segment boundary, covering the fact that
    # buffered audio lags control-channel timestamps by ~1.5s (grant->speech
    # offset measured 0.84-1.62s across 7 field calls).
    #
    # Still needed, with a narrower job than before. It no longer pads the
    # normal end of a call — that boundary now comes from the audio itself and
    # needs no pad at all. It applies to the three boundaries that are still
    # control-channel timestamps:
    #
    #   tgid_change            close the outgoing call at the new grant + pad
    #   tgid_change_unlogged   close at the observing poll + pad
    #   idle_timeout           console fallback mode only
    #
    # Safe to be generous: trim_silence strips trailing silence back to
    # trim_silence_guard_seconds before upload, so a larger pad costs long calls
    # nothing. Over-capture is free; under-capture loses words permanently. If
    # the outgoing and incoming recordings overlap in the underlying audio
    # because of this pad, that is correct — the audio contains both.
    call_tail_pad_seconds: float = 3.0

    # Strip leading/trailing dead air before upload. A recording deliberately
    # over-captures at both ends (pre-roll at the head, the whole measured
    # silence run at the tail), which inflates Whisper cost and is a
    # well-documented trigger for hallucinated transcript text. Trimming is a
    # sample-offset slice of the buffered PCM — no re-encode — and only ever
    # touches the head and tail. See internal/audio_trim.py.
    trim_silence: bool = True
    # dBFS (RMS) below which audio counts as silence when trimming the ends.
    # Kept above call_silence_threshold_db on purpose: the closer must not miss
    # speech (permissive), the trimmer must not leave dead air (stricter), and
    # trim_silence_guard_seconds protects the syllable either way.
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
