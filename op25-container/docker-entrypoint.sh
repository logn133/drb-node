#!/bin/bash

# --- Start PulseAudio Daemon ---
# The -D flag starts it as a daemon. 
# The --exit-idle-time=-1 prevents it from automatically shutting down.
echo "Starting PulseAudio daemon..."
pulseaudio -D --exit-idle-time=-1 --system

# Wait a moment for PulseAudio to initialize
sleep 1

# --- Execute the main command (uvicorn) ---
echo "Starting FastAPI application..."
# The main application arguments are passed directly to this script
exec "$@"