#!/bin/sh
# Supervisor for Trunk Recorder inside its container (node-26#45).
#
# The edge-node writes /app/config.json from the C2 system config but has no
# Docker access, so it cannot restart this container. This loop does it:
#   - waits for config.json, then runs trunk-recorder on it
#   - restarts it when config.json changes or /app/restart-request appears
#   - keeps it stopped while /app/stop-request exists (dashboard Stop)
#   - restarts it after a crash, backing off 5 s -> 60 s (`restarts` counts
#     crashes only, not config restarts)
#   - writes /app/drb-status.json every few seconds for the edge-node's health
#     check (state, pid, started_at, restarts, last_exit, epoch)
set -u
APP="${DRB_TR_APP:-/app}"
CFG="$APP/config.json"
STATUS="$APP/drb-status.json"
RESTART="$APP/restart-request"
STOP="$APP/stop-request"
REC="${DRB_RECORDINGS:-/recordings}"
mkdir -p "$APP/logs" "$REC/tr-capture" "$REC/tr-inbox"

pid=""
started=0
restarts=0
last_exit=null
backoff=5
cfgsum=""

write_status() {
  printf '{"state":"%s","pid":%s,"started_at":%s,"restarts":%s,"last_exit":%s,"epoch":%s}\n' \
    "$1" "${pid:-null}" "$started" "$restarts" "$last_exit" "$(date +%s)" > "$STATUS.tmp" \
    && mv -f "$STATUS.tmp" "$STATUS"
}

sum() { md5sum "$CFG" 2>/dev/null | cut -d' ' -f1; }

stop_tr() {
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    kill -TERM "$pid"
    i=0
    while kill -0 "$pid" 2>/dev/null && [ "$i" -lt 30 ]; do sleep 0.5; i=$((i + 1)); done
    kill -KILL "$pid" 2>/dev/null
    wait "$pid" 2>/dev/null
  fi
  pid=""
}

trap 'stop_tr; write_status stopped; exit 0' TERM INT

nap() { sleep "$1" & wait $!; }

while :; do
  if [ -f "$STOP" ]; then
    [ -n "$pid" ] && { echo "drb: stop requested"; stop_tr; }
    write_status stopped; nap 2; continue
  fi
  if [ ! -f "$CFG" ]; then
    write_status waiting_for_config; nap 5; continue
  fi
  if [ -z "$pid" ]; then
    rm -f "$RESTART"
    cfgsum="$(sum)"
    echo "drb: starting trunk-recorder"
    trunk-recorder --config="$CFG" &
    pid=$!
    started="$(date +%s)"
  fi
  write_status running
  nap 5
  if ! kill -0 "$pid" 2>/dev/null; then
    wait "$pid" 2>/dev/null
    last_exit=$?
    pid=""
    restarts=$((restarts + 1))
    # A run that lasted a while resets the backoff.
    [ $(( $(date +%s) - started )) -gt 120 ] && backoff=5
    echo "drb: trunk-recorder exited ($last_exit); restarting in ${backoff}s"
    write_status crashed
    nap "$backoff"
    backoff=$((backoff * 2)); [ "$backoff" -gt 60 ] && backoff=60
    continue
  fi
  if [ -f "$RESTART" ] || [ "$(sum)" != "$cfgsum" ]; then
    echo "drb: config changed or restart requested — restarting trunk-recorder"
    stop_tr
  fi
done
