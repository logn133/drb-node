#!/usr/bin/env bash
#
# Install (or refresh) the DRB host updater units (node-26#15). Run as root.
#
#   install-updater.sh --dir /opt/drb/node-26 --repo-url URL --ref v1 \
#                      --registry git.vpn.cusano.net [--build]
#   install-updater.sh --refresh      # re-render units from /etc/drb/updater.conf
#
# Writes /etc/drb/updater.conf (root:root 0644 — the ONLY source of what the
# updater may install), drb-update.{service,timer,path}, and enables the timer
# and the path watcher. install.sh calls this; drb-update.sh calls --refresh
# after a checkout so unit changes ship with the repo.
set -euo pipefail

CONF=/etc/drb/updater.conf
UNIT_DIR="${DRB_UNIT_DIR:-/etc/systemd/system}"
conf() { grep -E "^$1=" "$CONF" 2>/dev/null | head -1 | cut -d= -f2- || true; }
die()  { printf 'install-updater: %s\n' "$*" >&2; exit 1; }

INSTALL_DIR="" REPO_URL="" REF="" REGISTRY="" BUILD=0 REFRESH=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dir)       INSTALL_DIR="$2"; shift 2 ;;
    --repo-url)  REPO_URL="$2"; shift 2 ;;
    --ref)       REF="$2"; shift 2 ;;
    --registry)  REGISTRY="$2"; shift 2 ;;
    --build)     BUILD=1; shift ;;
    --refresh)   REFRESH=1; shift ;;
    *)           die "unknown option: $1" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "must run as root"
if [ "$REFRESH" = 1 ]; then
  [ -r "$CONF" ] || die "$CONF missing — run install.sh"
  INSTALL_DIR="$(conf INSTALL_DIR)"; REPO_URL="$(conf REPO_URL)"; REF="$(conf REF)"
  REGISTRY="$(conf REGISTRY)"; BUILD="$(conf BUILD)"
fi

[[ "$INSTALL_DIR" =~ ^/[A-Za-z0-9._/-]+$ ]] || die "bad --dir '$INSTALL_DIR' (absolute, no spaces)"
[[ "$REPO_URL" =~ ^https://[A-Za-z0-9._/:-]+$ ]] || die "--repo-url must be https: '$REPO_URL'"
[[ "$REF" =~ ^[A-Za-z0-9._][A-Za-z0-9._/-]*$ ]] || die "bad --ref '$REF'"
[[ "$REGISTRY" =~ ^[A-Za-z0-9.-]+(:[0-9]+)?$ ]] || die "bad --registry '$REGISTRY'"
[ "$BUILD" = 1 ] || BUILD=0

if ! command -v systemctl >/dev/null 2>&1 || [ ! -d /run/systemd/system ]; then
  die "systemd not running — the host updater needs systemd (remote updates disabled on this node)"
fi

install -d -m 755 /etc/drb
umask 022
cat > "$CONF.tmp" <<EOF
# DRB host updater — written by install-updater.sh. The updater installs ONLY
# from these values; nothing C2 sends can change them. Edit = root only.
INSTALL_DIR=$INSTALL_DIR
REPO_URL=$REPO_URL
REF=$REF
REGISTRY=$REGISTRY
BUILD=$BUILD
EOF
chown root:root "$CONF.tmp"; chmod 644 "$CONF.tmp"; mv -f "$CONF.tmp" "$CONF"

cat > "$UNIT_DIR/drb-update.service" <<EOF
[Unit]
Description=DRB node updater (node-26#15)
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service

[Service]
Type=oneshot
ExecStart=/bin/bash $INSTALL_DIR/scripts/drb-update.sh
TimeoutStartSec=$([ "$BUILD" = 1 ] && echo 3h || echo 45min)
Nice=10
EOF

cat > "$UNIT_DIR/drb-update.timer" <<'EOF'
[Unit]
Description=DRB node updater — periodic HTTPS check-in with C2

[Timer]
OnBootSec=3min
OnUnitActiveSec=15min
RandomizedDelaySec=2min
Persistent=true

[Install]
WantedBy=timers.target
EOF

cat > "$UNIT_DIR/drb-update.path" <<EOF
[Unit]
Description=DRB node updater — MQTT node_update sentinel

[Path]
PathExists=$INSTALL_DIR/configs/update-requested
Unit=drb-update.service

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now drb-update.timer drb-update.path >/dev/null 2>&1 \
  || die "could not enable drb-update.timer/path"
printf 'install-updater: host updater enabled (timer every ~15 min + MQTT sentinel), tracking %s @ %s\n' \
  "$REPO_URL" "$REF"
