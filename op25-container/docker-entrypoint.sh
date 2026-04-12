#!/bin/bash

# --- Start PulseAudio Daemon ---
# -n: skip default config (load modules inline — avoids system.pa parsing issues)
# --system: run as system-wide daemon
# --log-target=stderr: makes errors visible in Docker logs
# &: background so this script continues; output still captured by Docker
echo "Starting PulseAudio daemon..."
mkdir -p /run/pulse
chmod 777 /run/pulse
pulseaudio --exit-idle-time=-1 -n --system \
    --load="module-native-protocol-unix socket=/run/pulse/native auth-anonymous=1" \
    --load="module-null-sink sink_name=drb_sink sink_properties=device.description=DRB-Sink" \
    --log-target=stderr &

# Wait for the socket to actually exist before continuing
echo "Waiting for PulseAudio socket..."
for i in $(seq 1 20); do
    if [ -S /run/pulse/native ]; then
        echo "PulseAudio socket ready."
        break
    fi
    sleep 0.5
done
if [ ! -S /run/pulse/native ]; then
    echo "WARNING: PulseAudio socket not found after 10s — edge-node audio will fail."
fi
ls -la /run/pulse/

# --- Execute the main command (uvicorn) ---
echo "Starting FastAPI application..."
exec "$@"