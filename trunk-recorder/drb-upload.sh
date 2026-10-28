#!/bin/sh
# Trunk Recorder uploadScript (node-26#45). TR runs this after every call as
#     drb-upload.sh <call.wav> <call.json> <call.m4a>
# (run_upload_script_argv, call_concluder.cc v5.2.1; the m4a only exists with
# compressWav, which DRB leaves off). TR deletes the wav/json as soon as this
# exits (audioArchive/callLog false), so it copies them into the edge-node's
# inbox first. Order matters: the WAV lands first and the JSON last, so the
# JSON's presence tells the edge-node the WAV is complete. Exit non-zero = TR
# marks the call failed.
set -u
WAV="${1:?wav path}"
JSON="${2:?json path}"
INBOX="${DRB_TR_INBOX:-/recordings/tr-inbox}"
TMP="$INBOX/.incoming"

[ -f "$WAV" ] && [ -f "$JSON" ] || { echo "drb-upload: missing $WAV or $JSON" >&2; exit 1; }
mkdir -p "$TMP" || exit 1

stem="$(basename "$JSON" .json)"
# Hard link when on the same filesystem (no copy, TR's unlink leaves ours);
# copy otherwise.
put() { ln -f "$1" "$2" 2>/dev/null || cp -f "$1" "$2"; }

put "$WAV" "$TMP/$stem.wav" || exit 1
put "$JSON" "$TMP/$stem.json" || exit 1
mv -f "$TMP/$stem.wav" "$INBOX/$stem.wav" || exit 1
mv -f "$TMP/$stem.json" "$INBOX/$stem.json" || exit 1
exit 0
