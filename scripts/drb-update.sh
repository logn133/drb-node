#!/usr/bin/env bash
#
# DRB node host updater (node-26#15). Runs as root from systemd:
#   drb-update.timer  -> every ~15 min: HTTPS check-in with C2; update if asked,
#                        or if the configured channel has a newer release
#                        (node-26#49: REF=latest follows the highest vX.Y.Z
#                        tag, REF=v1 follows the promoted major tag)
#                        The portal can pick the channel per node: C2's
#                        check-in answer may say channel=stable|nightly and
#                        auto_update=true|false (see "Channels" below)
#   drb-update.path   -> fires when the edge-node container drops
#                        <install>/configs/update-requested (MQTT node_update)
# Both start drb-update.service, which runs this script. install-updater.sh
# writes the units and /etc/drb/updater.conf; install.sh calls it.
#
# SECURITY MODEL — this is a remote-update path onto devices in the field:
#   * What gets installed comes ONLY from /etc/drb/updater.conf (root-owned,
#     written by install.sh): the repo URL, the ref, the image registry. C2's
#     answer is a timestamp meaning "update now" plus, optionally, one of two
#     fixed channel words; neither is ever used as a ref, URL, path or
#     command — "nightly" maps to REF=latest and "stable" to the highest
#     promoted vN tag, both on the configured repo. The MQTT sentinel's
#     content is never read.
#   * git origin must equal the configured REPO_URL (https only) and the .env
#     IMAGE_REGISTRY must equal the configured REGISTRY, or nothing runs.
#   * Node credentials go to curl via a 0600 header file, never argv.
#   * C2 must be https.
#   * Release tags must be SSH-signed by a key in the root-owned allowed-
#     signers file (/etc/drb/release-signers, written by install-updater.sh;
#     node-26#17, server-26#318). Once that file exists, only a signed tag is
#     ever checked out — never a branch, never an unsigned or forged tag — so a
#     compromised git host cannot ship code. Until it exists (a node that has
#     not yet taken the update that installs it), the updater runs unsigned
#     and says so on every run.
# What this does NOT yet defend against: a compromised registry. Images are
# pulled by src- tag, not by digest from a signed manifest.
#   * Images are pinned to the checked-out source (node-26#19): each one is
#     pulled as src-<git tree hash of its build directory>, never :latest.
#
# The whole body is inside main(), called on the last line: bash parses the
# function completely before running it, so `git checkout` replacing this
# very file mid-run cannot change what is executing.
set -euo pipefail

# ── Release versions (node-26#49) ─────────────────────────────────────────
# Versions are MAJOR.MINOR.PATCH and every comparison is per field, as
# integers: 1.10.0 > 1.9.3. Never string or float order ("1.10" < "1.9" as a
# float, "1.10.0" < "1.9.3" as a string).
semver_sort() { sort -t. -k1,1n -k2,2n -k3,3n; }

semver_gt() {  # semver_gt A B -> true when A is newer than B
  local a b i
  IFS=. read -r -a a <<< "$1"; IFS=. read -r -a b <<< "$2"
  for i in 0 1 2; do
    (( 10#${a[i]:-0} > 10#${b[i]:-0} )) && return 0
    (( 10#${a[i]:-0} < 10#${b[i]:-0} )) && return 1
  done
  return 1
}

version_at() {  # highest X.Y.Z release tagged on a commit, or nothing
  git tag --points-at "$1" 2>/dev/null | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' \
    | sed 's/^v//' | semver_sort | tail -1 || true
}

# ── Release signatures (node-26#17) ─────────────────────────────────────────
SIGNERS="${DRB_UPDATER_SIGNERS:-/etc/drb/release-signers}"
# Written alongside SIGNERS and never removed: once a node has enforced
# signatures, a missing or emptied signers file fails CLOSED, not open.
SIGNING_REQUIRED="${DRB_UPDATER_SIGNING_REQUIRED:-/etc/drb/signing-required}"
# SSH tag signatures need git >= 2.34 and OpenSSH >= 8.2 (`ssh-keygen -Y`).
# A node below that cannot verify anything, and enforcing there would strand
# it for good — the release that fixes it could never install. Those nodes
# run unsigned and say so loudly; it is a local fact a git host cannot fake.
# Same check as install-updater.sh, which only installs the key when it passes.
can_verify_ssh_sigs() {
  local g o
  g="$(git --version 2>/dev/null | sed -n 's/^git version \([0-9]*\)\.\([0-9]*\).*/\1 \2/p')"
  o="$(ssh -V 2>&1 | sed -n 's/^OpenSSH_\([0-9]*\)\.\([0-9]*\).*/\1 \2/p')"
  [ -n "$g" ] && [ -n "$o" ] || return 1
  set -- $g $o
  { [ "$1" -gt 2 ] || { [ "$1" -eq 2 ] && [ "$2" -ge 34 ]; }; } \
    && { [ "$3" -gt 8 ] || { [ "$3" -eq 8 ] && [ "$4" -ge 2 ]; }; }
}
signing_enforced() { can_verify_ssh_sigs && { [ -s "$SIGNERS" ] || [ -e "$SIGNING_REQUIRED" ]; }; }

tag_is_signed() {  # tag_is_signed NAME -> true only for a tag signed by a pinned key
  [ -s "$SIGNERS" ] || return 1
  git -c gpg.format=ssh -c gpg.ssh.allowedSignersFile="$SIGNERS" \
    verify-tag "refs/tags/$1" >/dev/null 2>&1 || return 1
  # The signature covers the tag object, not the ref name: a git host could
  # serve an old signed release under a newer name. The signed `tag` header
  # must match the name it is served under.
  [ "$(git cat-file tag "refs/tags/$1" 2>/dev/null | sed -n 's/^tag //p' | head -1)" = "$1" ]
}

pick_top() {  # stdin: version-sorted tag names, oldest first -> newest usable one
  local t
  while IFS= read -r t; do
    if ! signing_enforced || tag_is_signed "$t"; then echo "$t"; return 0; fi
    printf 'drb-update: skipping unsigned or untrusted tag %s\n' "$t" >&2
  done < <(tac)
  return 1
}

resolve_target() {  # REF -> a ref name git can resolve, or fail
  local ref="$1" top
  if [ "$ref" = stable ]; then   # the highest promoted major tag (v1, v2, ...)
    top="$(git tag -l 'v*' | grep -E '^v[0-9]+$' | sed 's/^v//' | sort -n | sed 's/^/v/' | pick_top)" || return 1
    echo "refs/tags/$top"
  elif [ "$ref" = latest ]; then
    top="$(git tag -l 'v*' | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sed 's/^v//' | semver_sort | sed 's/^/v/' | pick_top)" \
      || return 1
    echo "refs/tags/$top"
  elif signing_enforced; then   # a named ref: must be a tag, and signed
    git show-ref --verify -q "refs/tags/$ref" && tag_is_signed "$ref" || return 1
    echo "refs/tags/$ref"
  elif git show-ref --verify -q "refs/remotes/origin/$ref"; then
    echo "refs/remotes/origin/$ref"
  elif git show-ref --verify -q "refs/tags/$ref"; then
    echo "refs/tags/$ref"
  else
    return 1
  fi
}

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

  if ! can_verify_ssh_sigs; then
    log "WARNING: git/OpenSSH too old to verify release signatures (need git>=2.34, OpenSSH>=8.2) — NOT checked (node-26#17)"
  elif ! signing_enforced; then
    log "WARNING: $SIGNERS missing — release signatures NOT checked (node-26#17)"
  fi

  mkdir -p "$STATE_DIR"; chmod 700 "$STATE_DIR"
  exec 9>"$LOCK"
  flock -n 9 || { log "another update is already running"; return 0; }

  cd "$INSTALL_DIR"
  # This runs as root, but a pre-install.sh checkout (e.g. /home/pi/node-26)
  # is owned by the login user, and git >= 2.35.2 then refuses every command
  # with "dubious ownership" — which surfaced as origin '' != REPO_URL. Trust
  # exactly the configured INSTALL_DIR, for this process only (env config is
  # command-line scope, which safe.directory honours; no global config change).
  export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0="$INSTALL_DIR"
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
    # Host facts for the signing rollout (node-26#17): lets the portal find
    # nodes whose git is too old to verify release signatures.
    local osc gitv sig
    osc="$(. /etc/os-release 2>/dev/null; printf '%s' "${VERSION_CODENAME:-}")"
    gitv="$(git --version 2>/dev/null | sed -n 's/^git version \([0-9][0-9A-Za-z._-]*\).*/\1/p')"
    if ! can_verify_ssh_sigs; then sig=unsigned_old_git
    elif signing_enforced; then sig=enforced
    else sig=unsigned_no_key; fi
    body="$(jq -nc --arg sha "$sha" --arg a "$1" --arg r "$2" \
      --arg osc "$osc" --arg gitv "$gitv" --arg sig "$sig" \
      '{sha:$sha, signing:$sig} + (if $osc != "" then {os_codename:$osc} else {} end)
                  + (if $gitv != "" then {git_version:$gitv} else {} end)
                  + (if $a != "" then {applied_request_at:$a} else {} end)
                  + (if $r != "" then {result:$r} else {} end)')"
    curl -fsS --proto '=https' --max-time 20 -X POST \
      "$C2_URL/nodes/$NODE_ID/update-check" \
      -H "Content-Type: application/json" -H "@$tmpd/h" --data "$body"
  }

  local resp=""
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

  # ── Channels ───────────────────────────────────────────────────────────
  # The portal's per-node choice, remembered so an offline C2 doesn't flip a
  # node back to its install-time REF. Anything but the exact words is ignored.
  local channel="" auto_update=""
  if [ -n "${resp:-}" ]; then
    channel="$(printf '%s' "$resp" | jq -r '.channel // empty' 2>/dev/null || true)"
    auto_update="$(printf '%s' "$resp" | jq -r 'if .auto_update == false then "false" elif .auto_update == true then "true" else empty end' 2>/dev/null || true)"
    [[ "$channel" =~ ^(stable|nightly)$ ]] && printf '%s' "$channel" > "$STATE_DIR/channel"
    [[ "$auto_update" =~ ^(true|false)$ ]] && printf '%s' "$auto_update" > "$STATE_DIR/auto_update"
  fi
  channel="$(cat "$STATE_DIR/channel" 2>/dev/null || true)"
  auto_update="$(cat "$STATE_DIR/auto_update" 2>/dev/null || true)"
  case "$channel" in
    nightly) REF=latest ;;
    stable)  REF=stable ;;
  esac

  local last=""; last="$(cat "$STATE_DIR/last_request" 2>/dev/null || true)"
  local new_request=""
  if [ -n "$requested" ] && [ "$requested" != "$last" ]; then
    new_request="$requested"
    reason="${reason:-C2 request $requested}"
  fi
  # A newer release on this node's channel is a reason on its own (node-26#49).
  # Only ever forward, and only between tagged releases: an untagged target
  # (a branch REF, or v1 before its first promotion) still needs a request.
  if [ -z "$reason" ] && [ "$auto_update" = false ]; then
    log "auto-update is off for this node — updating only when asked"
  elif [ -z "$reason" ]; then
    local origin_url target tv iv
    origin_url="$(git remote get-url origin 2>/dev/null || true)"
    if [ "$origin_url" = "$REPO_URL" ] \
       && git -c protocol.allow=never -c protocol.https.allow=always fetch -q --force --tags --prune origin \
       && target="$(resolve_target "$REF")"; then
      tv="$(version_at "$(git rev-parse "$target^{commit}")")"
      iv="$(version_at HEAD)"
      if [ -n "$tv" ] && { [ -z "$iv" ] || semver_gt "$tv" "$iv"; }; then
        reason="release $tv on $REF (installed ${iv:-untagged})"
      fi
    fi
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
  target="$(resolve_target "$REF")" \
    || { err "no usable ref '$REF' on origin (missing, or not signed by a key in $SIGNERS)"; return 1; }
  if signing_enforced; then
    [[ "$target" == refs/tags/* ]] && tag_is_signed "${target#refs/tags/}" \
      || { err "target $target is not a signed release tag — refusing"; return 1; }
  fi
  local old new
  old="$(git rev-parse HEAD)"; new="$(git rev-parse "$target^{commit}")"
  # Forward only: a replayed old signed release must not roll a node back to
  # code with a known hole. A real rollback is a new signed release.
  if signing_enforced && [ "$old" != "$new" ] && ! git merge-base --is-ancestor "$old" "$new" 2>/dev/null; then
    err "target ${new:0:12} is not ahead of installed ${old:0:12} — refusing a rollback"; return 1
  fi

  local env_registry
  env_registry="$(grep -E '^IMAGE_REGISTRY=' .env | head -1 | cut -d= -f2- | tr -d '"' || true)"
  [ "${env_registry:-git.vpn.cusano.net}" = "$REGISTRY" ] \
    || { err ".env IMAGE_REGISTRY '$env_registry' != configured '$REGISTRY' — refusing"; return 1; }

  # Image pins for the TARGET commit (node-26#19): each image is tagged with
  # the git tree hash of its own source directory, so the images always match
  # the code being checked out. The helper is read from the target commit so
  # its service list matches the target's compose file.
  # Read into a variable, not `source <(...)`: a failed `git show` must leave
  # pin=0 rather than "succeed" with no functions defined.
  local pin=0 helper
  if git cat-file -e "$new:scripts/image-tags.sh" 2>/dev/null; then
    helper="$(git show "$new:scripts/image-tags.sh")" && [ -n "$helper" ] && eval "$helper" \
      && declare -F drb_image_tags drb_images_present drb_pin_image_tags >/dev/null && pin=1
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

  # The release this node now runs, for its checkin (node-26#49).
  local ver; ver="$(version_at "$new")"
  sed -i '/^DRB_VERSION=/d' "$INSTALL_DIR/.env"
  printf 'DRB_VERSION=%s\n' "${ver:-untagged}" >> "$INSTALL_DIR/.env"

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

  # node-26#41: neither of the above ever freed BuildKit cache or untagged-
  # but-unused images, so a BUILD=1 node grew ~40 GB of build cache plus
  # 16 GB of stale images until the root disk hit 100% and recording silently
  # stopped (node-002, 17 days). Both prunes skip anything a container uses;
  # the age filters keep the cache warm for the next build.
  docker builder prune -af --filter until=72h >/dev/null 2>&1 || true
  docker image prune -af --filter until=168h >/dev/null 2>&1 || true
}

main "$@"
