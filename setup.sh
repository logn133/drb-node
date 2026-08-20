#!/usr/bin/env bash
# Interactive first-time setup for a DRB edge node.
# Installs system dependencies (Docker, make, curl) then writes .env
# and optionally builds + starts the stack.
set -e

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; RED='\033[0;31m'; NC='\033[0m'
cd "$(dirname "$0")"

echo -e "${CYAN}DRB Edge Node Setup${NC}"
echo "-------------------"

# ── Dependency installation ──────────────────────────────────────────────────
install_deps() {
  if ! command -v apt-get &>/dev/null; then
    echo -e "${YELLOW}⚠  apt-get not found — skipping auto-install. Ensure docker, make, and curl are installed.${NC}"
    return
  fi

  echo ""
  echo -e "${CYAN}Installing system dependencies…${NC}"
  sudo apt-get update -qq

  local pkgs=()
  command -v make &>/dev/null  || pkgs+=(make)
  command -v curl &>/dev/null  || pkgs+=(curl)
  command -v git  &>/dev/null  || pkgs+=(git)

  if [ ${#pkgs[@]} -gt 0 ]; then
    echo "  Installing: ${pkgs[*]}"
    sudo apt-get install -y -qq "${pkgs[@]}"
  fi

  # Docker — use get.docker.com if not present
  if ! command -v docker &>/dev/null; then
    echo "  Installing Docker via get.docker.com…"
    curl -fsSL https://get.docker.com | sudo sh
    # Allow current user to run docker without sudo
    sudo usermod -aG docker "$USER"
    echo -e "${YELLOW}  ⚠  Docker group added. You may need to log out and back in for it to take effect.${NC}"
    echo -e "${YELLOW}     If 'docker compose' fails below, run: newgrp docker${NC}"
  else
    echo -e "${GREEN}  ✓ docker$(docker --version | grep -oP ' \d+\.\d+\.\d+' | head -1)${NC}"
  fi

  # Docker Compose plugin check (comes with Docker Engine ≥ 20.10)
  if ! docker compose version &>/dev/null 2>&1; then
    echo -e "${RED}  docker compose plugin not found. Installing…${NC}"
    sudo apt-get install -y -qq docker-compose-plugin
  else
    echo -e "${GREEN}  ✓ docker compose $(docker compose version --short 2>/dev/null || true)${NC}"
  fi

  echo -e "${GREEN}✓ Dependencies ready${NC}"
}

install_deps

if [ -f .env ]; then
  echo -e "${YELLOW}Warning: .env already exists.${NC}"
  read -rp "Overwrite? [y/N] " yn
  [[ "$yn" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }
fi

# --- Node identity ---
echo ""
echo "Unique node ID — no spaces (e.g. node-ossining, node-002)"
read -rp "NODE_ID: " NODE_ID
while [[ ! "$NODE_ID" =~ ^[a-zA-Z0-9_-]+$ ]]; do
  echo "  Use letters, numbers, dashes, underscores only."
  read -rp "NODE_ID: " NODE_ID
done

echo ""
read -rp "Node display name [$NODE_ID]: " NODE_NAME
NODE_NAME="${NODE_NAME:-$NODE_ID}"

# --- GPS ---
echo ""
echo "GPS coordinates (decimal degrees — used for the map)"
read -rp "Latitude  [0.0]: " NODE_LAT; NODE_LAT="${NODE_LAT:-0.0}"
read -rp "Longitude [0.0]: " NODE_LON; NODE_LON="${NODE_LON:-0.0}"

# --- C2 server ---
echo ""
echo "C2 server — hostname or IP of the machine running the server stack"
read -rp "C2 server host: " C2_HOST; C2_HOST="${C2_HOST:-localhost}"
read -rp "C2 API port [8888]: " C2_PORT; C2_PORT="${C2_PORT:-8888}"

# --- MQTT ---
echo ""
echo "MQTT credentials (must match MQTT_NODE_USER/PASS in the server .env)"
read -rp "MQTT port [1883]: " MQTT_PORT; MQTT_PORT="${MQTT_PORT:-1883}"
read -rp "MQTT username [drb-node]: " MQTT_USER; MQTT_USER="${MQTT_USER:-drb-node}"
read -rsp "MQTT password: " MQTT_PASS; echo ""; MQTT_PASS="${MQTT_PASS:-change-me-node}"

# --- Icecast ---
# Generated, not defaulted. Icecast binds all interfaces, and the SOURCE
# password is what lets a caller push audio into the stream users listen to as
# live radio -- so a shared default is worse here than a forgotten password.
# Pressing Enter gives you a random one rather than a known one.
gen_password() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -base64 24 | tr -d '
'
  else
    head -c 24 /dev/urandom | base64 | tr -d '
'
  fi
}

echo ""
echo "Icecast passwords (local container). Press Enter to generate a random one."
read -rsp "Source password [generate]: " ICECAST_SOURCE; echo ""
if [ -z "$ICECAST_SOURCE" ]; then ICECAST_SOURCE="$(gen_password)"; echo "  generated a random source password"; fi
read -rsp "Admin password [generate]: "  ICECAST_ADMIN;  echo ""
if [ -z "$ICECAST_ADMIN" ];  then ICECAST_ADMIN="$(gen_password)";  echo "  generated a random admin password"; fi

# --- Write .env ---
cat > .env <<EOF
# Node Identity
NODE_ID=${NODE_ID}
NODE_NAME="${NODE_NAME}"
NODE_LAT=${NODE_LAT}
NODE_LON=${NODE_LON}

# MQTT — point to your C2 server
MQTT_BROKER=${C2_HOST}
MQTT_PORT=${MQTT_PORT}
MQTT_USER=${MQTT_USER}
MQTT_PASS=${MQTT_PASS}

# C2 server for audio upload
C2_URL=http://${C2_HOST}:${C2_PORT}
# API key is provisioned automatically via MQTT after admin approves the node

# Icecast (local container — usually no need to change)
ICECAST_SOURCE_PASSWORD=${ICECAST_SOURCE}
ICECAST_ADMIN_PASSWORD=${ICECAST_ADMIN}
ICECAST_HOST=localhost
ICECAST_PORT=8000
ICECAST_MOUNT=/radio

# OP25 container (usually no need to change)
OP25_API_URL=http://localhost:8001
OP25_TERMINAL_URL=http://localhost:8081
EOF

echo ""
echo -e "${GREEN}✓ .env written for node '${NODE_ID}'${NC}"
echo ""

read -rp "Build and start now? [Y/n] " start
if [[ ! "$start" =~ ^[Nn]$ ]]; then
  echo ""
  echo "Building images (op25 takes ~10 min on first run)…"
  docker compose build
  docker compose up -d
  echo ""
  echo -e "${GREEN}✓ Node '${NODE_ID}' started.${NC}"
  echo "   → Check the dashboard — it will appear as pending approval."
else
  echo "Run 'make up' when ready."
fi
