#!/usr/bin/env bash
#
# DRB node host updater (node-26#15). Runs as root from systemd:
#   drb-update.timer  -> every ~15 min: HTTPS check-in with C2; update if asked
#   drb-update.path   -> fires when the edge-node container drops
#                        <install>/configs/update-requested (MQTT node_update)
# Both start drb-update.service, which runs this script. install-updater.sh
# writes the units and /etc/drb/updater.conf; install.sh calls it.
#
# SECURITY MODEL — this is a remote-update path onto devices in the field:
#   * What gets installed comes ONLY from /etc/drb/updater.conf (root-owned,
#     written by install.sh): the repo URL, the ref, the image registry. C2's
#     answer is a single timestamp meaning "update now"; it is never used as a
#     ref, URL, path or command. The MQTT sentinel's content is never read.
#   * git origin must equal the configured REPO_URL (https only) and the .env
#     IMAGE_REGISTRY must equal the configured REGISTRY, or nothing runs.
#   * Node credentials go to curl via a 0600 header file, never argv.
#   * C2 must be https.
# What this does NOT defend against: a compromised git host or registry —
# refs and images are not signed (node-26#17).
#   * Images are pinned to the checked-out source (node-26#19): each one is
#     pulled as src-<git tree hash of its build directory>, never :latest.
#
# The whole body is inside main(), called on the last line: bash parses the
# function completely before running it, so `git checkout` replacing this
# very file mid-run cannot change what is executing.
set -euo pipefail

main() {
  local CONF="${DRB_UPDATER_CONF:-/etc/drb/updater.conf}"
  local STATE_DIR="${DRB_UPDATER_STATE:-/var/lib/drb-update}"
  local LOCK="${DRB_UPDATER_LOCK:-/run/drb-update.lock}"

  log()  { printf 'drb-update: %s\n' "$*"; }
  err()  { printf 'drb-update: ERROR: %s\n' "$*" >&2; }
  conf() { grep -E "^$1=" "$CONF" 2>/dev/null | head -1 | cut -d= -f2- || true; }

  [ -r "$CONF" ] || { err "$CONF missing — re-run install.sh"; return 1; }
  local INSTALL_DIR REPO_URL REF REGISTRY BUILD
  INSTALL_DIR="$(conf INSTALL_DIR)"; REPO_URL="$(conf REPO_URL)"; REF="$(conf REF)"
  REGISTRY="$(conf REGISTRY)"; BUILD="$(conf BUILD)"

  [[ "$INSTALL_DIR" =~ ^/[A-Za-z0-9._/-]+$ ]] && [ -d "$INSTALL_DIR/.git" ] \
    || { err "bad INSTALL_DIR '$INSTALL_DIR' in $CONF"; return 1; }
  [[ "$REPO_URL" =~ ^https://[A-Za-z0-9._/:-]+$ ]] || { err "REPO_URL must be https: '$REPO_URL'"; return 1; }
  [[ "$REF" =~ ^[A-Za-z0-9._][A-Za-z0-9._/-]*$ ]] || { err "bad REF '$REF'"; return 1; }
  [[ "$REGISTRY" =~ ^[A-Za-z0-9.-]+(:[0-9]+)?$ ]] || { err "bad REGISTRY '$REGISTRY'"; return 1; }

  mkdir -p "$STATE_DIR"; chmod 700 "$STATE_DIR"
  exec 9>"$LOCK"
  flock -n 9 || { log "another update is already running"; return 0; }

  cd "$INSTALL_DIR"
  envget() { grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' || true; }
  local NODE_ID C2_URL
  NODE_ID="$(envget NODE_ID)"; C2_URL="$(envget C2_URL)"; C2_URL="${C2_URL%/}"
  [[ "$NODE_ID" =~ ^[A-Za-z0-9_-]+$ ]] || { err "no valid NODE_ID in $INSTALL_DIR/.env"; return 1; }

  local reason=""
  local SENTINEL="$INSTALL_DIR/configs/update-requested"
  if [ -e "$SENTINEL" ] || [ -L "$SENTINEL" ]; then
    rm -f "$SENTINEL"          # content deliberately ignored
    reason="mqtt node_update"
  fi

  # ── HTTPS check-in ──────────────────────────────────────────────────────
  local tmpd; tmpd="$(mktemp -d)"; chmod 700 "$tmpd"
  # shellcheck disable=SC2064
  trap "rm -rf '$tmpd'" RETURN

  local sha; sha="$(git rev-parse HEAD 2>/dev/null || echo '')"
  local requested=""

  checkin() {  # checkin APPLIED_AT RESULT -> stdout: response body
    [[ "$C2_URL" == https://* ]] || return 1
    local key="" pickup=""
    key="$(jq -r '.api_key // empty' configs/credentials.json 2>/dev/null || true)"
    pickup="$(cat configs/pickup_secret 2>/dev/null || true)"
    : > "$tmpd/h"; chmod 600 "$tmpd/h"
    if [[ "$key" =~ ^[A-Za-z0-9]+$ ]]; then
      printf 'Authorization: Bearer %s\n' "$key" >> "$tmpd/h"
    fi
    if [[ "$pickup" =~ ^[A-Za-z0-9]+$ ]]; then
      printf 'X-Pickup-Secret: %s\n' "$pickup" >> "$tmpd/h"
    fi
    [ -s "$tmpd/h" ] || return 1
    local body
    body="$(jq -nc --arg sha "$sha" --arg a "$1" --arg r "$2" \
      '{sha:$sha} + (if $a != "" then {applied_request_at:$a} else {} end)
                  + (if $r != "" then {result:$r} else {} end)')"
    curl -fsS --proto '=https' --max-time 20 -X POST \
      "$C2_URL/nodes/$NODE_ID/update-check" \
      -H "Content-Type: application/json" -H "@$tmpd/h" --data "$body"
  }

  local resp
  if [[ "$C2_URL" != https://* ]]; then
    log "C2_URL is not https — skipping the HTTPS check-in"
  elif resp="$(checkin "" "" 2>"$tmpd/err")"; then
    requested="$(printf '%s' "$resp" | jq -r '.update_requested_at // empty' 2>/dev/null || true)"
    if [ -n "$requested" ] && ! [[ "$requested" =~ ^[0-9T:.+Z-]{10,40}$ ]]; then
      log "ignoring malformed update_requested_at from C2"
      requested=""
    fi
  else
    log "HTTPS check-in failed: $(tr -d '\n' < "$tmpd/err" | head -c 200)"
  fi

  local last=""; last="$(cat "$STATE_DIR/last_request" 2>/dev/null || true)"
  local new_request=""
  if [ -n "$requested" ] && [ "$requested" != "$last" ]; then
    new_request="$requested"
    reason="${reason:-C2 request $requested}"
  fi
  [ -n "$reason" ] || { log "no update requested"; return 0; }

  # ── Update ─────────────────────────────────────────────────────────────
  log "updating ($reason) — repo $REPO_URL @ $REF"
  local result=ok
  if do_update; then
    log "update complete: $(git rev-parse --short HEAD)"
  else
    result=failed
    log "update FAILED — will retry on the next timer run"
  fi
  if [ -n "$new_request" ]; then
    [ "$result" = ok ] && printf '%s' "$new_request" > "$STATE_DIR/last_request"
    sha="$(git rev-parse HEAD 2>/dev/null || echo '')"
    checkin "$new_request" "$result" >/dev/null 2>&1 || log "could not report the result to C2"
  fi
  [ "$result" = ok ]
}

do_update() {
  local origin; origin="$(git remote get-url origin)"
  [ "$origin" = "$REPO_URL" ] || { err "git origin '$origin' != configured '$REPO_URL' — refusing"; return 1; }

  # --force: a moved release tag (v1) must replace the local copy.
  git -c protocol.allow=never -c protocol.https.allow=always fetch --force --tags --prune origin || return 1

  local target
  if git show-ref --verify -q "refs/remotes/origin/$REF"; then
    target="refs/remotes/origin/$REF"
  elif git show-ref --verify -q "refs/tags/$REF"; then
    target="refs/tags/$REF"
  else
    err "ref '$REF' not found on origin"; return 1
  fi
  local old new
  old="$(git rev-parse HEAD)"; new="$(git rev-parse "$target^{commit}")"

  local env_registry
  env_registry="$(grep -E '^IMAGE_REGISTRY=' .env | head -1 | cut -d= -f2- | tr -d '"' || true)"
  [ "${env_registry:-git.vpn.cusano.net}" = "$REGISTRY" ] \
    || { err ".env IMAGE_REGISTRY '$env_registry' != configured '$REGISTRY' — refusing"; return 1; }

  # Image pins for the TARGET commit (node-26#19): each image is tagged with
  # the git tree hash of its own source directory, so the images always match
  # the code being checked out. The helper is read from the target commit so
  # its service list matches the target's compose file.
  local pin=0
  if git cat-file -e "$new:scripts/image-tags.sh" 2>/dev/null; then
    # shellcheck disable=SC1090
    source <(git show "$new:scripts/image-tags.sh") && pin=1
  fi
  [ "$pin" = 1 ] || log "target ${new:0:12} predates image pinning — using its compose file's default tags"

  if [ "$BUILD" != 1 ]; then
    # Pull BEFORE checkout. The app code is bind-mounted from the checkout
    # (install.sh D3) and edge-node runs uvicorn --reload, so checking out first
    # would run the new code on the old images for as long as the pull takes —
    # or for good, if the pinned images don't exist. Pulling against the
    # target's own compose file means a failed pull changes nothing. It is
    # written next to the real one so its relative paths (.env, env_file,
    # build contexts) resolve exactly the same way.
    local tfile="$INSTALL_DIR/.drb-update-compose.yml" pulled=0
    git show "$new:docker-compose.yml" > "$tfile" || { rm -f "$tfile"; return 1; }
    local -a tc=(-f "$tfile" --project-directory "$INSTALL_DIR")
    (
      if [ "$pin" = 1 ]; then
        while IFS= read -r l; do export "$l"; done < <(drb_image_tags "$INSTALL_DIR" "$new")
      fi
      docker compose "${tc[@]}" pull || exit 1
      if [ "$pin" = 1 ]; then drb_images_present "${tc[@]}" || exit 1; fi
    ) && pulled=1
    rm -f "$tfile"
    [ "$pulled" = 1 ] || { err "images for ${new:0:12} are not available — nothing changed (CI may still be building them; the 'Release images' workflow publishes any that are missing)"; return 1; }
  fi

  if [ "$old" != "$new" ]; then
    git -c advice.detachedHead=false checkout --force --detach "$new" || return 1
    log "checked out $REF: ${old:0:12} -> ${new:0:12}"
    # Units/conf may have changed with the new scripts.
    bash scripts/install-updater.sh --refresh || log "unit refresh failed (continuing with the old units)"
  fi

  if [ "$pin" = 1 ]; then
    drb_pin_image_tags "$INSTALL_DIR/.env" "$INSTALL_DIR" "$new" || { err "could not write image pins to .env"; return 1; }
  fi

  if [ "$BUILD" = 1 ]; then
    docker compose build || return 1   # tags the local builds with the same pins
  fi
  # --force-recreate: app code is bind-mounted from the checkout (install.sh
  # D3), so a code-only change also needs every container restarted.
  docker compose up -d --no-build --force-recreate --remove-orphans || return 1

  # Drop pinned images this node no longer runs. `image prune` only removes
  # dangling images, and every previous src- tag is still tagged. `image rm`
  # without -f refuses anything a container still uses.
  if [ "$pin" = 1 ]; then
    local keep ref
    keep="$(drb_image_tags "$INSTALL_DIR" "$new" | cut -d= -f2)"
    docker images --format '{{.Repository}}:{{.Tag}}' | grep -E ':src-[0-9a-f]{40}$' \
      | while IFS= read -r ref; do
          grep -qxF "${ref##*:}" <<< "$keep" || docker image rm "$ref" >/dev/null 2>&1 || true
        done || true
  fi
  docker image prune -f >/dev/null 2>&1 || true
}

main "$@"
