#!/bin/bash

# Configuration file path
CONFIG_FILE="/configs/active.cfg.json"

# --- Start the main OP25 receiver (multi_rx.py) in the background ---
# The '&' sends the process to the background.
echo "Starting multi_rx.py..."
./multi_rx.py -v 1 -c $CONFIG_FILE &
MULTI_RX_PID=$! # Store the PID of the background process

# --- Start the liquid-dsp plot utility (op25.liq) in the background ---
echo "Starting op25.liq..."
liquidsoap /configs/op25.liq &
LIQ_PID=$! # Store the PID of the op25.liq process

# Wait for both background jobs to finish.
# Since multi_rx.py is the core service, this script will effectively wait 
# until multi_rx.py is externally stopped (via the API).
# The trap command ensures that SIGTERM is passed to the background jobs.
trap "kill $MULTI_RX_PID $LIQ_PID" SIGTERM SIGINT
wait $MULTI_RX_PID
RC=$?

# multi_rx is gone (crashed, or SDR could not be opened). Take liquidsoap down
# with it so nothing in this process group outlives the decoder — a leftover
# liquidsoap kept /op25/status reporting "running" (node-26#12) and held the
# Icecast mount against the next start.
echo "multi_rx.py exited (code $RC) — stopping liquidsoap."
kill $LIQ_PID 2>/dev/null
exit $RC