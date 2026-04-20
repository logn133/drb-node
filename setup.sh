#!/usr/bin/env bash
# Interactive first-time setup for a DRB edge node.
# Writes .env and optionally builds + starts the stack.
set -e

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
cd "$(dirname "$0")"

echo -e "${CYAN}DRB Edge Node Setup${NC}"
echo "-------------------"

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
echo ""
echo "Icecast passwords (local container)"
read -rsp "Source password [hackme]: " ICECAST_SOURCE; echo ""; ICECAST_SOURCE="${ICECAST_SOURCE:-hackme}"
read -rsp "Admin password [admin]: "  ICECAST_ADMIN;  echo ""; ICECAST_ADMIN="${ICECAST_ADMIN:-admin}"

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
