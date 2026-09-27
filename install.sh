#!/usr/bin/env bash
#
# DRB edge node — one-shot bootstrap for a clean Raspberry Pi OS (arm64).
#
#   curl -fsSL https://git.vpn.cusano.net/logan/node-26/raw/tag/v1/install.sh \
#     | sudo bash -s -- --token DRB-xxxx --node-id node-003 \
#         --c2-url https://api.<domain> --mqtt-broker mqtt.<domain>
#
# (fetch install.sh from the same tag it installs; use raw/branch/main with
#  --track-main for the owner's own always-latest nodes)
#
# Hosting, the pinned ref, and setup.sh's retirement are settled (owner
# decisions, 2026-09-06). One standing hazard remains — D3 below.
#
# What it does, in order:
#   1. Preflight (root, arch, apt, SDR present)
#   2. Install Docker + compose plugin + git + curl + jq
#   3. Clone logan/node-26 at a PINNED ref into $INSTALL_DIR
#   4. Write .env (non-interactive from flags/env, interactive fallback)
#   5. Enroll with C2 (POST /nodes/enroll) and poll for the api_key
#   6. docker compose pull && up -d   (prebuilt; --build opts into the ~1h build)
#   7. Print the approval step
#
# It does NOT set up WireGuard. WireGuard-per-node was evaluated and rejected
# (Server/MQTT-PUBLIC-AUTH-PLAN.md, Server/infra/main.tf:67-73). A field node
# reaches production over the public internet only:
#     https://api.<domain>      enrollment + /upload   (Caddy, real TLS)
#     mqtt.<domain>:8883        MQTT over TLS, username=NODE_ID password=api_key
# The two stale "nodes reach it via WireGuard" comments in
# Server/docker-compose.prod.yml:6 and Server/infra/main.tf:69 are leftovers.
#
# ---------------------------------------------------------------------------
# SETTLED (owner decisions, 2026-09-06 — node-26#4)
#
#   D1  HOSTING. git.vpn.cusano.net is PUBLIC — it is a CNAME to
#       cusano-net.duckdns.org (71.117.95.129) with a real Let's Encrypt cert,
#       resolvable from any public resolver. install.sh, `git clone` and
#       `docker compose pull` all work from a customer's Pi with no VPN.
#       Caveat, not a blocker: that IP is a dynamic-DNS record on the owner's
#       home uplink, so it is a single point of failure and a bandwidth limit
#       — fine for the beachhead, revisit before scaling node count.
#
#   D2  PIN. node-26 gets a `v1` tag (owner cuts it — see the git command in
#       the mint panel / node-26#4). DEFAULT_REF below is `v1`. `--track-main`
#       stays as an opt-in for the owner's own nodes.
#
# STANDING HAZARD
#
#   D3  SOURCE-OVERLAY. docker-compose.yml bind-mounts ./drb-edge-node/app and
#       ./op25-container/app OVER the image's /app. So even in prebuilt mode
#       the running Python is the CLONED REF's code against the pulled image's
#       dependencies. `v1` == the commit CI built the current :latest/:stable
#       from, so today they match — but the moment `v1` and the image tags
#       diverge this silently mixes them. Fix: drop those two mounts from the
#       prod compose path, or always retag images from the same ref as `v1`.
# ---------------------------------------------------------------------------
set -euo pipefail

# ── Defaults ────────────────────────────────────────────────────────────────
DEFAULT_REF="v1"                                                  # D2
DEFAULT_REPO_URL="https://git.vpn.cusano.net/logan/node-26.git"   # D1 (public)
DEFAULT_INSTALL_DIR="/opt/drb/node-26"

REPO_URL="${DRB_REPO_URL:-$DEFAULT_REPO_URL}"
REF="${DRB_REF:-$DEFAULT_REF}"
INSTALL_DIR="${DRB_INSTALL_DIR:-$DEFAULT_INSTALL_DIR}"

NODE_ID="${DRB_NODE_ID:-}"
NODE_NAME="${DRB_NODE_NAME:-}"
NODE_LAT="${DRB_NODE_LAT:-}"
NODE_LON="${DRB_NODE_LON:-}"
C2_URL="${DRB_C2_URL:-}"
MQTT_BROKER="${DRB_MQTT_BROKER:-}"
MQTT_PORT="${DRB_MQTT_PORT:-8883}"
MQTT_TLS="${DRB_MQTT_TLS:-true}"
ENROLLMENT_TOKEN="${DRB_ENROLLMENT_TOKEN:-}"
DASHBOARD_USER="${DRB_DASHBOARD_USERNAME:-admin}"
DASHBOARD_PASS="${DRB_DASHBOARD_PASSWORD:-}"
REGISTRY="${DRB_IMAGE_REGISTRY:-git.vpn.cusano.net}"   # D1 (public host)
DOCKER_ORG="${DRB_DOCKER_ORG:-logan}"
DOCKER_REPO="${DRB_DOCKER_REPO:-node-26}"
REGISTRY_USER="${DRB_REGISTRY_USER:-}"
REGISTRY_PASS="${DRB_REGISTRY_PASS:-}"

DO_BUILD=0        # 0 = pull prebuilt images (default), 1 = build on the Pi (~1h for op25)
DO_START=1
ASSUME_YES=0
ENROLL_WAIT="${DRB_ENROLL_WAIT:-0}"   # seconds to block waiting for admin approval; 0 = don't block

C='\033[0;36m'; G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; N='\033[0m'
say()  { printf "${C}==>${N} %s\n" "$*"; }
ok()   { printf "${G}  ok${N} %s\n" "$*"; }
warn() { printf "${Y}  !!${N} %s\n" "$*" >&2; }
die()  { printf "${R}error:${N} %s\n" "$*" >&2; exit 1; }

usage() {
  cat <<'USAGE'
Usage: install.sh [options]

  --token TOKEN        Enrollment token (Settings -> Nodes -> New token)
  --node-id ID         Unique node id, e.g. node-003
  --name NAME          Display name (default: node id)
  --lat N --lon N      Decimal degrees for the map
  --c2-url URL         e.g. https://api.drb.example.net
  --mqtt-broker HOST   e.g. mqtt.drb.example.net
  --mqtt-port N        default 8883
  --no-tls             plaintext MQTT (LAN/dev brokers only)
  --dashboard-pass PW  local dashboard password (generated if omitted)
  --ref REF            git ref to install (default: v1)
  --track-main         install main HEAD instead of the v1 tag
  --dir PATH           install location (default /opt/drb/node-26)
  --build              build images locally instead of pulling (~1h for op25)
  --no-start           configure and enroll, but do not start containers
  --wait-approval SEC  block up to SEC seconds polling for admin approval
  -y, --yes            never prompt; fail instead of asking

Every option also has an env var: DRB_NODE_ID, DRB_C2_URL, DRB_ENROLLMENT_TOKEN,
DRB_MQTT_BROKER, DRB_REF, DRB_INSTALL_DIR, DRB_REGISTRY_USER/PASS, ...
Secrets are read from the environment or prompted on the TTY, never from a pipe.
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --token)          ENROLLMENT_TOKEN="$2"; shift 2 ;;
    --node-id)        NODE_ID="$2"; shift 2 ;;
    --name)           NODE_NAME="$2"; shift 2 ;;
    --lat)            NODE_LAT="$2"; shift 2 ;;
    --lon)            NODE_LON="$2"; shift 2 ;;
    --c2-url)         C2_URL="$2"; shift 2 ;;
    --mqtt-broker)    MQTT_BROKER="$2"; shift 2 ;;
    --mqtt-port)      MQTT_PORT="$2"; shift 2 ;;
    --no-tls)         MQTT_TLS=false; [ "$MQTT_PORT" = 8883 ] && MQTT_PORT=1883; shift ;;
    --dashboard-pass) DASHBOARD_PASS="$2"; shift 2 ;;
    --ref)            REF="$2"; shift 2 ;;
    --track-main)     REF="main"; shift ;;
    --dir)            INSTALL_DIR="$2"; shift 2 ;;
    --build)          DO_BUILD=1; shift ;;
    --no-start)       DO_START=0; shift ;;
    --wait-approval)  ENROLL_WAIT="$2"; shift 2 ;;
    -y|--yes)         ASSUME_YES=1; shift ;;
    -h|--help)        usage; exit 0 ;;
    *)                die "unknown option: $1 (try --help)" ;;
  esac
done

# Prompts must come from the terminal, not from the `curl |` pipe on stdin.
ask() {  # ask VAR "prompt" "default"
  local __v="$1" __p="$2" __d="${3:-}" __r=""
  if [ "$ASSUME_YES" = 1 ] || [ ! -r /dev/tty ]; then
    [ -n "$__d" ] || die "$__p is required (non-interactive: pass the flag or env var)"
    printf -v "$__v" '%s' "$__d"; return
  fi
  read -rp "$__p${__d:+ [$__d]}: " __r </dev/tty
  printf -v "$__v" '%s' "${__r:-$__d}"
}
ask_secret() {
  local __v="$1" __p="$2" __r=""
  if [ "$ASSUME_YES" = 1 ] || [ ! -r /dev/tty ]; then printf -v "$__v" '%s' ''; return; fi
  read -rsp "$__p: " __r </dev/tty; echo >/dev/tty
  printf -v "$__v" '%s' "$__r"
}
genpw() { head -c 24 /dev/urandom | base64 | tr -d '\n=' ; }

# ── 1. Preflight ────────────────────────────────────────────────────────────
say "Preflight"
[ "$(id -u)" -eq 0 ] || die "run as root: curl -fsSL <url> | sudo bash -s -- ..."
command -v apt-get >/dev/null || die "apt-get not found — this script targets Raspberry Pi OS / Debian"

ARCH="$(dpkg --print-architecture)"
case "$ARCH" in
  arm64|aarch64) ok "arch $ARCH" ;;
  *) warn "arch is $ARCH — CI only builds linux/arm64 images. Prebuilt pull will fail; use --build." ;;
esac
ok "running as root"

# Not fatal: the dongle can be plugged in after install.
if command -v lsusb >/dev/null 2>&1 && lsusb | grep -qiE 'rtl2838|realtek.*283[28]|sdr'; then
  ok "SDR dongle detected on USB"
else
  warn "no RTL-SDR dongle detected on USB — plug one in before expecting audio"
fi

# The kernel's DVB-TV driver auto-binds RTL2838 dongles. op25 detaches it from
# the one it opens, but any other dongle stays claimed and the secondary-sdr
# decoder fails with "usb_claim_interface error -6" (node-26#9).
echo "blacklist dvb_usb_rtl28xxu" > /etc/modprobe.d/blacklist-rtl-sdr.conf
modprobe -r rtl2832_sdr dvb_usb_rtl28xxu 2>/dev/null || true
ok "DVB-TV kernel driver blacklisted for RTL-SDR"

# ── 2. Dependencies ─────────────────────────────────────────────────────────
say "Installing dependencies"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git curl ca-certificates jq usbutils openssl >/dev/null
ok "git curl jq openssl"

if ! command -v docker >/dev/null 2>&1; then
  say "Installing Docker (get.docker.com)"
  curl -fsSL https://get.docker.com | sh
fi
docker --version >/dev/null || die "docker install failed"
ok "$(docker --version)"

if ! docker compose version >/dev/null 2>&1; then
  apt-get install -y -qq docker-compose-plugin >/dev/null
fi
docker compose version >/dev/null 2>&1 || die "docker compose plugin missing"
ok "$(docker compose version --short 2>/dev/null || echo 'compose plugin')"

systemctl enable --now docker >/dev/null 2>&1 || true

# The docker group only matters for the human who logs in LATER — this script
# is already root, so nothing below needs a re-login. setup.sh's bug (add the
# group, then immediately run compose in the same unprivileged shell) does not
# apply here.
TARGET_USER="${SUDO_USER:-}"
if [ -n "$TARGET_USER" ] && [ "$TARGET_USER" != root ]; then
  usermod -aG docker "$TARGET_USER" || true
  ok "added '$TARGET_USER' to the docker group (takes effect at their next login)"
fi

# ── 3. Fetch the repo at a pinned ref ───────────────────────────────────────
say "Fetching node-26 @ ${REF}"
mkdir -p "$(dirname "$INSTALL_DIR")"
if [ -d "$INSTALL_DIR/.git" ]; then
  ok "existing install at $INSTALL_DIR — updating in place (.env is preserved)"
  git -C "$INSTALL_DIR" remote set-url origin "$REPO_URL"
  git -C "$INSTALL_DIR" fetch --tags --prune origin
else
  git clone --no-checkout "$REPO_URL" "$INSTALL_DIR"
fi
git -C "$INSTALL_DIR" -c advice.detachedHead=false checkout --force "$REF"
RESOLVED_SHA="$(git -C "$INSTALL_DIR" rev-parse HEAD)"
ok "checked out $REF ($RESOLVED_SHA)"
[ "$REF" = main ] && warn "tracking main — two nodes installed on different days run different software"

cd "$INSTALL_DIR"
mkdir -p configs recordings
chmod 700 configs

# ── 4. Configuration ────────────────────────────────────────────────────────
say "Configuring"
if [ -f .env ]; then
  ok ".env already present — keeping it (delete it to reconfigure)"
  # Re-read the three values section 5 needs. Not `source .env` — that would
  # execute whatever is in the file.
  envget() { grep -E "^$1=" .env | head -1 | cut -d= -f2- | tr -d '"'; }
  NODE_ID="$(envget NODE_ID)"
  C2_URL="${C2_URL:-$(envget C2_URL)}"; C2_URL="${C2_URL%/}"
  NODE_NAME="${NODE_NAME:-$(envget NODE_NAME)}"
  NODE_LAT="${NODE_LAT:-$(envget NODE_LAT)}"
  NODE_LON="${NODE_LON:-$(envget NODE_LON)}"
  DASHBOARD_USER="$(envget DASHBOARD_USERNAME)"
  [ -n "$NODE_ID" ] || die ".env exists but has no NODE_ID — fix or delete it"
else
  [ -n "$NODE_ID" ] || ask NODE_ID "Node ID (e.g. node-003)"
  [[ "$NODE_ID" =~ ^[A-Za-z0-9_-]+$ ]] || die "NODE_ID must be letters/numbers/dash/underscore only"
  [ -n "$NODE_NAME" ] || ask NODE_NAME "Display name" "$NODE_ID"
  [ -n "$NODE_LAT" ]  || ask NODE_LAT  "Latitude"  "0.0"
  [ -n "$NODE_LON" ]  || ask NODE_LON  "Longitude" "0.0"
  [ -n "$C2_URL" ]    || ask C2_URL    "C2 API base URL (https://api.<domain>)"
  C2_URL="${C2_URL%/}"
  [ -n "$MQTT_BROKER" ] || ask MQTT_BROKER "MQTT broker host (mqtt.<domain>)"

  if [ -z "$DASHBOARD_PASS" ]; then
    ask_secret DASHBOARD_PASS "Local dashboard password (Enter to generate)"
    [ -n "$DASHBOARD_PASS" ] || { DASHBOARD_PASS="$(genpw)"; GENERATED_DASH=1; }
  fi
  ICE_SRC="$(genpw)"; ICE_ADM="$(genpw)"

  umask 077
  cat > .env <<EOF
# Written by install.sh on $(date -Is) from ref ${RESOLVED_SHA}
NODE_ID=${NODE_ID}
NODE_NAME="${NODE_NAME}"
NODE_LAT=${NODE_LAT}
NODE_LON=${NODE_LON}

# MQTT — post-cutover auth. There is NO shared node login: the node
# authenticates as username=NODE_ID, password=<its C2-issued api_key>, which
# section 5 below fetches into configs/credentials.json. Deliberately no
# MQTT_USER/MQTT_PASS here; a dynsec broker rejects them.
MQTT_BROKER=${MQTT_BROKER}
MQTT_PORT=${MQTT_PORT}
MQTT_TLS=${MQTT_TLS}

C2_URL=${C2_URL}

ICECAST_SOURCE_PASSWORD=${ICE_SRC}
ICECAST_ADMIN_PASSWORD=${ICE_ADM}
ICECAST_HOST=localhost
ICECAST_PORT=8000
ICECAST_MOUNT=/radio

DASHBOARD_USERNAME=${DASHBOARD_USER}
DASHBOARD_PASSWORD=${DASHBOARD_PASS}

PULSE_SOURCE=drb_sink.monitor
OP25_API_URL=http://localhost:8001
OP25_TERMINAL_URL=http://localhost:8081
OP25_DEBUG_EXPOSE=false

IMAGE_REGISTRY=${REGISTRY}
DOCKER_ORG=${DOCKER_ORG}
DOCKER_REPO=${DOCKER_REPO}
EOF
  umask 022
  chmod 600 .env
  [ -n "$TARGET_USER" ] && chown "$TARGET_USER" .env 2>/dev/null || true
  ok ".env written for '$NODE_ID'"
fi

# ── 5. Enrollment ───────────────────────────────────────────────────────────
# Client half of Server/drb-c2-core/app/routers/enrollment.py. It does NOT
# exist in the edge-node app today (mqtt_manager.py:73-85 says so explicitly),
# so without this section a fresh node can never obtain an api_key against a
# dynsec broker: MQTT needs the key, and the legacy key-over-MQTT delivery
# needs MQTT. Doing it here breaks that loop.
#
# Two server endpoints, and the exact response shapes verified against
# enrollment.py @ v1:
#
#   POST /nodes/enroll   (X-Enrollment-Token)
#     200 -> {node_id, pickup_secret, approval_status}
#     403 -> node_id is ALREADY APPROVED. The CRITICAL GUARD in enrollment.py
#            refuses to mint a fresh pickup_secret off the shared fleet token.
#            So we must only ever POST this for a node we have not enrolled
#            from this machine before — i.e. when configs/pickup_secret is
#            absent. Re-running the installer must NOT re-POST here.
#     401 bad/revoked token · 400 missing node_id · 429 rate limited
#
#   GET /nodes/{id}/credentials   (X-Pickup-Secret)
#     Always HTTP 200 with {approval_status, api_key} unless the secret or
#     node is bad. api_key is null until an admin approves the node in the UI
#     (nodes.py approve_node() mints node_keys/{id}.api_key synchronously in
#     the same call — approve is enough; assigning a system is independent and
#     NOT required for a key). This endpoint has NO already-approved guard, so
#     it is the correct — and only working — re-run path after approval.
#     401 -> missing/invalid/rotated pickup secret
#     404 -> node unknown to C2 (deleted server-side, or never enrolled)
CREDS="$INSTALL_DIR/configs/credentials.json"
PICKUP_FILE="$INSTALL_DIR/configs/pickup_secret"

# GET /nodes/{id}/credentials. Sets CRED_HTTP + CRED_BODY (no -f: we need the
# body and status on a 4xx). One implementation so first-run and re-run agree.
creds_pickup() {  # creds_pickup PICKUP_SECRET
  local _tmp; _tmp="$(mktemp)"
  CRED_HTTP="$(curl -sS -o "$_tmp" -w '%{http_code}' \
      "$C2_URL/nodes/$NODE_ID/credentials" -H "X-Pickup-Secret: $1" 2>/dev/null || echo 000)"
  CRED_BODY="$(cat "$_tmp" 2>/dev/null || true)"
  rm -f "$_tmp"
}
cred_field() { printf '%s' "${CRED_BODY:-}" | jq -r "$1 // empty" 2>/dev/null || true; }

write_creds() {  # write_creds API_KEY
  umask 077; jq -n --arg k "$1" '{api_key:$k}' > "$CREDS"; umask 022
  ok "api_key received and written to configs/credentials.json"
}

say_pending() {  # say_pending APPROVAL_STATUS  — not an error: node is enrolled, key not minted yet
  warn "not approved yet — C2 reports approval_status=${1:-pending}, no api_key minted."
  warn "an admin must, at  <app-url>/settings/nodes :  Approve '$NODE_ID'  (assigning a system is separate)."
  warn "then re-run this installer — it reuses configs/pickup_secret — or fetch it directly:"
  warn "  curl -fsS $C2_URL/nodes/$NODE_ID/credentials -H \"X-Pickup-Secret: \$(cat $PICKUP_FILE)\" | jq -r .api_key"
}

# Poll creds_pickup for up to ENROLL_WAIT seconds while still pending. Result
# left in CRED_HTTP/CRED_BODY. --wait-approval is what would have avoided the
# original prod bug; the default (0) does not block, so the re-run path below
# must stand on its own.
wait_for_key() {  # wait_for_key PICKUP_SECRET
  [ "${ENROLL_WAIT:-0}" -gt 0 ] || return 0
  local _end; _end=$(( $(date +%s) + ENROLL_WAIT ))
  say "Waiting up to ${ENROLL_WAIT}s for an admin to approve '$NODE_ID'"
  while [ "$(date +%s)" -lt "$_end" ]; do
    sleep 10
    creds_pickup "$1"
    [ "$CRED_HTTP" = 200 ] || return 0
    [ -z "$(cred_field '.api_key')" ] || return 0
  done
}

do_fresh_enroll() {
  say "Enrolling '$NODE_ID' with $C2_URL"
  [ -n "$ENROLLMENT_TOKEN" ] || ask_secret ENROLLMENT_TOKEN "Enrollment token"
  [ -n "$ENROLLMENT_TOKEN" ] || die "no enrollment token — mint one at Settings -> Nodes, then re-run with --token"

  local BODY RESP PICKUP STATUS KEY
  BODY="$(jq -nc --arg id "$NODE_ID" --arg n "${NODE_NAME:-$NODE_ID}" \
            --argjson lat "${NODE_LAT:-0}" --argjson lon "${NODE_LON:-0}" \
            '{node_id:$id,name:$n,lat:$lat,lon:$lon}')"
  # Token goes in a header from a shell variable — never on a process command
  # line, never echoed.
  if ! RESP="$(curl -fsS -X POST "$C2_URL/nodes/enroll" \
            -H "Content-Type: application/json" \
            -H "X-Enrollment-Token: $ENROLLMENT_TOKEN" \
            --data "$BODY" 2>&1)"; then
    case "$RESP" in
      *403*) die "enroll refused (403): node '$NODE_ID' is already approved on C2, and
  this machine has no configs/pickup_secret to collect its key with. A token
  alone cannot re-issue an approved node's key (enrollment.py CRITICAL GUARD).
  Recover by restoring this node's original configs/pickup_secret and re-running,
  or have an admin Reissue key (Settings -> Nodes) and write the key into
  $CREDS by hand (see node-26#6)." ;;
      *401*) die "enroll refused (401): enrollment token missing/invalid/revoked —
  mint a fresh one at Settings -> Nodes and re-run with --token." ;;
      *429*) die "enroll refused (429): rate limited. Wait ~1 minute, then re-run." ;;
      *)     die "enroll failed: $RESP" ;;
    esac
  fi
  PICKUP="$(printf '%s' "$RESP" | jq -r '.pickup_secret')"
  STATUS="$(printf '%s' "$RESP" | jq -r '.approval_status')"
  [ -n "$PICKUP" ] && [ "$PICKUP" != null ] || die "enroll returned no pickup_secret: $RESP"

  umask 077; printf '%s' "$PICKUP" > "$PICKUP_FILE"; umask 022
  ok "enrolled — approval_status=$STATUS (pickup secret saved to configs/pickup_secret)"

  creds_pickup "$PICKUP"
  [ "$CRED_HTTP" = 200 ] || die "post-enroll credential fetch failed (HTTP $CRED_HTTP): ${CRED_BODY:-<no body>}"
  KEY="$(cred_field '.api_key')"
  if [ -z "$KEY" ]; then
    wait_for_key "$PICKUP" || true
    KEY="$(cred_field '.api_key')"
  fi
  if [ -n "$KEY" ]; then
    write_creds "$KEY"
  else
    say_pending "$(cred_field '.approval_status')"
  fi
}

if [ -s "$CREDS" ] && jq -e '.api_key // empty' "$CREDS" >/dev/null 2>&1; then
  say "Enrollment"
  ok "api_key already on disk — skipping enrollment"
elif [ -z "${C2_URL:-}" ]; then
  say "Enrollment"
  warn "no C2_URL — skipping enrollment"
elif [ -s "$PICKUP_FILE" ]; then
  # RE-RUN. This node already enrolled from this machine. Do NOT POST
  # /nodes/enroll again — an approved node_id gets 403 there, and the v1
  # installer's own "re-run to pick up the key" advice then dead-ends on
  # "use Reissue key". The pickup endpoint has no such guard: use it.
  say "Enrollment — collecting credentials for '$NODE_ID' (pickup secret from a previous run)"
  PICKUP="$(cat "$PICKUP_FILE")"
  creds_pickup "$PICKUP"
  case "$CRED_HTTP" in
    200)
      API_KEY="$(cred_field '.api_key')"
      if [ -z "$API_KEY" ]; then
        wait_for_key "$PICKUP" || true
        API_KEY="$(cred_field '.api_key')"
      fi
      if [ -n "$API_KEY" ]; then
        write_creds "$API_KEY"
      else
        # Still pending. Enrolled and idempotent — next run collects the key.
        # Clean exit, fall through to start the stack. NOT a failure.
        say_pending "$(cred_field '.approval_status')"
      fi
      ;;
    401)
      if [ -n "$ENROLLMENT_TOKEN" ]; then
        warn "saved pickup secret rejected (401) — likely rotated by a re-enroll elsewhere. Re-enrolling with --token."
        rm -f "$PICKUP_FILE"
        do_fresh_enroll
      else
        die "saved pickup secret is stale (401) and no --token was given. Re-run with
  --token DRB-… to re-enroll (only works while the node is still pending), or
  have an admin Reissue key for an approved node and write $CREDS by hand."
      fi
      ;;
    404)
      if [ -n "$ENROLLMENT_TOKEN" ]; then
        warn "C2 does not know node '$NODE_ID' (404) — deleted server-side or never fully enrolled. Re-enrolling with --token."
        rm -f "$PICKUP_FILE"
        do_fresh_enroll
      else
        die "C2 does not know node '$NODE_ID' (404) and no --token was given.
  Re-run with --token DRB-… to enroll it again."
      fi
      ;;
    000)
      die "could not reach $C2_URL/nodes/$NODE_ID/credentials — check --c2-url and connectivity." ;;
    *)
      die "credential pickup failed (HTTP $CRED_HTTP): ${CRED_BODY:-<no body>}" ;;
  esac
else
  say "Enrollment"
  do_fresh_enroll
fi

# ── 6. Images + start ───────────────────────────────────────────────────────
if [ "$DO_START" = 1 ]; then
  if [ -n "$REGISTRY_USER" ] && [ -n "$REGISTRY_PASS" ]; then
    printf '%s' "$REGISTRY_PASS" | docker login "$REGISTRY" -u "$REGISTRY_USER" --password-stdin >/dev/null
    ok "logged in to $REGISTRY"
  fi

  if [ "$DO_BUILD" = 1 ]; then
    say "Building images locally — op25 takes roughly an hour on a Pi"
    docker compose build
    docker compose up -d
  else
    say "Pulling prebuilt images from $REGISTRY/$DOCKER_ORG/$DOCKER_REPO"
    if ! docker compose pull; then
      die "pull failed. $REGISTRY is public, so this is most likely a login
  requirement or a transient network error: re-run with DRB_REGISTRY_USER /
  DRB_REGISTRY_PASS set, or with --build to compile on the Pi (~1h for op25)."
    fi
    docker compose up --no-build -d
  fi
  ok "stack started"
else
  say "Skipping start (--no-start). Run: cd $INSTALL_DIR && make up-prebuilt"
fi

# ── 7. What the operator does next ──────────────────────────────────────────
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
cat <<EOF

$(printf "${G}Node '%s' installed at %s${N}" "$NODE_ID" "$INSTALL_DIR")

  ref            ${RESOLVED_SHA}
  images         $([ "$DO_BUILD" = 1 ] && echo "built locally" || echo "pulled from $REGISTRY")
  dashboard      http://${IP:-<node-ip>}/    (user: ${DASHBOARD_USER})
  logs           cd $INSTALL_DIR && docker compose logs -f edge-node

NEXT — an admin must approve this node before it can do anything:

  1. Open  <app-url>/settings/nodes
  2. Approve  "$NODE_ID"
  3. Assign it a radio system

EOF
if [ "${GENERATED_DASH:-0}" = 1 ]; then
  printf "${Y}Generated dashboard password (shown once): %s${N}\n\n" "$DASHBOARD_PASS"
fi
if [ ! -s "$CREDS" ]; then
  if [ -s "$PICKUP_FILE" ]; then
    printf "${Y}This node has no api_key yet. After an admin approves it, re-run the same\ninstall command — it reuses configs/pickup_secret and will collect the key\n(no --token needed for the re-run).${N}\n\n"
  else
    printf "${Y}This node has no api_key and no saved pickup secret, so a plain re-run cannot\nfix it. Re-run with --token DRB-… to enroll; or, if the node is already\napproved, have an admin Reissue key and write it into\n%s by hand.${N}\n\n" "$CREDS"
  fi
fi
