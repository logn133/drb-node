#!/bin/bash

PULSE_SOCKET=/run/pulse/native
PULSE_PIDFILE=/run/pulse/pid

mkdir -p /run/pulse
chmod 777 /run/pulse

# Returns 0 (true) only when a PulseAudio daemon actually answers on
# $PULSE_SOCKET. A socket/pid FILE existing proves nothing by itself — that
# is exactly the bug this script works around (see stale-state check below).
pulse_daemon_alive() {
    PULSE_SERVER="unix:${PULSE_SOCKET}" timeout 2 pactl info >/dev/null 2>&1
}

# --- Clear stale PulseAudio state left behind by a killed daemon ---
# The `pulse_socket` named volume survives container recreation, but the
# PulseAudio process that owned it does not. If the previous container was
# recreated (not gracefully stopped), its pid file and native socket are
# still sitting in the volume; pulseaudio's pid.c sees the pid file and
# refuses to start ("Daemon already running") even though nothing is
# listening. Only remove these when nothing actually answers on the socket —
# never delete a socket a live daemon is using.
if [ -S "$PULSE_SOCKET" ] || [ -f "$PULSE_PIDFILE" ]; then
    if pulse_daemon_alive; then
        echo "PulseAudio daemon already alive and responding at ${PULSE_SOCKET} — leaving state as-is."
    else
        echo "STALE STATE: found ${PULSE_PIDFILE} / ${PULSE_SOCKET} from a previous container, but no daemon answers — clearing before start."
        rm -f "$PULSE_SOCKET" "$PULSE_PIDFILE"
    fi
fi

# --- Start PulseAudio Daemon ---
# -n: skip default config (load modules inline — avoids system.pa parsing issues)
# --system: run as system-wide daemon
# --log-target=stderr: makes errors visible in Docker logs
# &: background so this script continues; output still captured by Docker
echo "Starting PulseAudio daemon..."
pulseaudio --exit-idle-time=-1 -n --system \
    --load="module-native-protocol-unix socket=${PULSE_SOCKET} auth-anonymous=1" \
    --load="module-null-sink sink_name=drb_sink sink_properties=device.description=DRB-Sink" \
    --log-target=stderr &

# Wait for the daemon to actually answer — NOT just for the socket file to
# exist. A stale socket file from a killed daemon exists but nothing is
# listening on it; a file-existence check reports "ready" against a dead
# daemon, which is exactly how this class of bug slipped through before.
echo "Waiting for PulseAudio to become live..."
PULSE_LIVE=0
for i in $(seq 1 20); do
    if pulse_daemon_alive; then
        echo "PulseAudio daemon is live (pactl info succeeded)."
        PULSE_LIVE=1
        break
    fi
    sleep 0.5
done
if [ "$PULSE_LIVE" -ne 1 ]; then
    echo "WARNING: PulseAudio daemon not responding after 10s — edge-node audio will fail until it recovers."
fi
ls -la /run/pulse/

# --- Execute the main command (uvicorn) ---
echo "Starting FastAPI application..."
exec "$@"
