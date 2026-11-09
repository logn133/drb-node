from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    # Node identity
    node_id: str
    node_name: str = "Unnamed Node"
    # The release installed on this node (node-26#49). The host updater writes
    # DRB_VERSION to .env after each update; "untagged" before the first one.
    drb_version: str = "untagged"
    node_lat: float = 0.0
    node_lon: float = 0.0

    # MQTT
    #
    # Broker cutover (MQTT-PUBLIC-AUTH-PLAN.md, dynsec revision): the server no
    # longer has a shared node login. Each node authenticates as
    # username=NODE_ID, password=<its C2-issued api_key> (the same credential
    # /upload already trusts via node_keys) — see mqtt_manager._build_client().
    # For local dev against the old-style broker (localhost:1883, no TLS) set
    # MQTT_BROKER=localhost and leave MQTT_TLS unset/false.
    mqtt_broker: str
    mqtt_port: int = 1883
    # Set true for the public broker (mqtt.<domain>:8883, real Let's Encrypt
    # cert) so client.tls_set() runs with default system-CA verification.
    # False by default so local/dev against a plaintext :1883 broker still
    # works unchanged. Do NOT pair with a self-signed/insecure cert setup —
    # verification is never disabled (no tls_insecure_set(True) anywhere).
    mqtt_tls: bool = False
    # DEPRECATED / effectively dead post-cutover: the shared node login these
    # backed no longer exists on the server (dynsec has no such client — see
    # dynsec.py). Left in only as a legacy fallback for a pre-cutover broker
    # that still uses mosquitto's old password_file auth; mqtt_manager only
    # falls back to these when no api_key is on disk yet. Do not provision new
    # nodes with these — see MQTT_USER/MQTT_PASS removal note in .env.example.
    mqtt_user: Optional[str] = None
    mqtt_pass: Optional[str] = None

    # C2 server (audio upload destination); None disables upload
    c2_url: Optional[str] = None

    # Local Icecast — live listening only (frontend / mobile).
    # NOT used for call recording or Discord voice: it lags 1s and drifts to 100s+.
    icecast_host: str = "localhost"
    icecast_port: int = 8000
    icecast_mount: str = "/radio"
    # No default: see icecast/entrypoint.sh, which refuses to start without one.
    icecast_source_password: str = ""

    # PulseAudio — the low-latency path used for call recording and Discord voice.
    # Liquidsoap (op25 container) writes into the `drb_sink` null sink; we capture
    # its monitor. Addressed explicitly rather than via "default" because the op25
    # entrypoint starts pulseaudio with -n and never applies system.pa's
    # `set-default-source` line.
    pulse_source: str = "drb_sink.monitor"
    # Bounded wait for the shared PulseAudio socket before launching FFmpeg.
    pulse_wait_timeout: float = 30.0

    # Discord radio (node-26#25): with the broker unreachable this long, log
    # every radio bot out. MUST stay below C2's
    # DISCORD_LEASE_OFFLINE_RELEASE_SECONDS (default 600), after which C2 hands
    # this node's bot tokens to other nodes — so a token is never logged in on
    # two nodes at once.
    discord_mqtt_loss_stop_seconds: int = 300

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

    # Secondary SDR container (node-26#9) — ADS-B / AIS on a second SDR
    secondary_sdr_api_url: str = "http://localhost:8002"

    # Paths (volume mounts)
    config_path: str = "/configs"
    recordings_path: str = "/recordings"

    # Offline call buffer — how many call_end events to keep while disconnected
    offline_call_buffer_size: int = 35

    # Upload spool (node-26#44) — recordings whose upload to C2 failed are kept
    # here and retried instead of deleted. Defaults to <recordings_path>/spool,
    # i.e. on the host disk via the /recordings bind mount, so it survives a
    # restart. Bounded both ways because a full host disk stops recording
    # outright (node-26#41): FLAC is ~1.3 MB per minute of audio, so 512 MiB is
    # roughly 6 h of continuous traffic. Oldest entries are evicted first.
    upload_spool_dir: Optional[str] = None
    upload_spool_max_bytes: int = 512 * 1024 * 1024
    upload_spool_max_age_hours: float = 72.0
    # Exponential backoff between retries of one spooled recording.
    upload_retry_min_seconds: float = 15.0
    upload_retry_max_seconds: float = 600.0
    # Timeout for the FIRST, inline upload attempt. metadata_watcher awaits the
    # call close, so a black-holed C2 used to stall segmentation for the full
    # 120 s; a failure here just lands in the spool.
    upload_inline_timeout_seconds: float = 30.0

    # ------------------------------------------------------------------
    # Local dashboard / API authentication
    #
    # These nodes are deployed at arbitrary third-party locations, reachable by
    # anyone on that site's LAN — there is no auth on this HTTP surface without
    # these. The password below is a FIRST-BOOT DEFAULT ONLY: change it via
    # DASHBOARD_PASSWORD in .env before a node leaves the bench. main.py logs a
    # startup warning every boot the default is still active.
    #
    # See app/internal/auth.py — the password is never compared or stored in
    # plaintext (scrypt-hashed, constant-time compare); this setting just holds
    # the operator-facing plaintext the same way MQTT_PASS/ICECAST_* already do.
    # ------------------------------------------------------------------
    dashboard_username: str = "admin"
    dashboard_password: str = "CHANGE-ME-drb-default"

    class Config:
        env_file = ".env"


settings = Settings()
