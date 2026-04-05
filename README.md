# DRB Client (Edge Node)

The client-side stack for the Discord Radio Bot system. Runs on the SDR hardware machine. Decodes radio with OP25, streams audio via Icecast, and connects to the C2 server over MQTT.

## Services

| Service | Description |
|---|---|
| `op25` | OP25 SDR decoder — tunes to radio systems and streams decoded audio |
| `icecast` | Audio streaming server — receives audio from OP25 and serves it over HTTP |
| `edge-node` | FastAPI node agent — bridges OP25 metadata, MQTT C2 commands, and Discord voice |

All services run with `network_mode: host` so they share the host network and can reach each other on localhost.

## Prerequisites

- Docker + Docker Compose
- An RTL-SDR or compatible SDR dongle connected to the host
- Network access to the DRB server (C2 server)

## Setup

```bash
# 1. Copy env file
cp .env.example .env

# 2. Fill in .env
#    At minimum: NODE_ID, MQTT_BROKER (IP of the C2 server)

# 3. Build and start
docker compose build
docker compose up -d
```

## Environment Variables (`.env`)

| Variable | Description | Default |
|---|---|---|
| `NODE_ID` | Unique identifier for this node | required |
| `NODE_NAME` | Human-readable name shown in the UI | `My Radio Node` |
| `NODE_LAT` / `NODE_LON` | GPS coordinates for the map view | `0.0` |
| `MQTT_BROKER` | IP or hostname of the C2 server | `localhost` |
| `MQTT_PORT` | MQTT broker port | `1883` |
| `C2_URL` | C2 server HTTP API URL (for audio uploads) | — |
| `ICECAST_HOST` | Icecast hostname (leave as localhost) | `localhost` |
| `ICECAST_PORT` | Icecast port | `8000` |
| `ICECAST_MOUNT` | Icecast mount point | `/radio` |

## Node Provisioning Flow

1. Start the client stack — the edge node connects to MQTT and sends a **checkin**
2. The C2 server registers the node as **pending approval**
3. An admin approves the node in the web UI
4. The admin assigns a **radio system** to the node via the UI
5. The C2 server pushes the system config over MQTT
6. The edge node writes the config and **starts OP25**
7. OP25 begins decoding and streaming audio to Icecast

After a restart, OP25 resumes automatically if the node was already configured.

## Discord Voice

When a user runs `/join` in Discord (or clicks "Join Discord" in the UI), the C2 server sends a `discord_join` command to this node over MQTT. The edge node spins up a Discord bot using a token from the server's token pool and connects it to the requested voice channel, streaming live audio from Icecast.

## Logs

```bash
docker compose logs -f edge-node
docker compose logs -f op25
docker compose logs -f icecast
```
