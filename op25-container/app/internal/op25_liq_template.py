liquidsoap_config_template = """#!/usr/bin/liquidsoap

# OP25 → Icecast streaming (Liquidsoap 2.x)

settings.log.stdout.set(true)
settings.log.file.set(false)
settings.log.level.set(1)
settings.frame.audio.samplerate.set(8000)
settings.init.allow_root.set(true)

# ==========================================================
ICE_HOST = "${icecast_host}"
ICE_PORT = ${icecast_port}
ICE_MOUNT = "${icecast_mountpoint}"
ICE_PASSWORD = "${icecast_password}"
ICE_DESCRIPTION = "${icecast_description}"
ICE_GENRE = "${icecast_genre}"
# ==========================================================

input = mksafe(input.external(buffer=0.25, channels=2, samplerate=8000, restart_on_error=false, "./audio.py -x 2.5 -s"))
# Consider increasing the buffer value on slow systems such as RPi3. e.g. buffer=0.25

# Compression
input = compress(input, attack = 2.0, gain = 0.0, knee = 13.0, ratio = 2.0, release = 12.3, threshold = -18.0)

# Normalization
input = normalize(input, gain_max = 6.0, gain_min = -6.0, target = -16.0, threshold = -65.0)

# ==========================================================
# OUTPUTS
# ==========================================================

# Primary: PulseAudio null sink — used by edge-node for Discord streaming and call recording.
# The edge-node reads from the drb_sink.monitor source (set as PulseAudio default source).
output.pulseaudio(mean(input))

# Secondary: Icecast — for browser-based listening only (local node UI / server stream page).
# Do NOT use Icecast for recording or Discord; it introduces 2-5s buffering lag.
output.icecast(
    %mp3(bitrate=16, samplerate=22050, stereo=false),
    description=ICE_DESCRIPTION,
    genre=ICE_GENRE,
    url="",
    fallible=false,
    host=ICE_HOST,
    port=ICE_PORT,
    mount=ICE_MOUNT,
    password=ICE_PASSWORD,
    mean(input)
)
"""