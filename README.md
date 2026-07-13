# DRB Client (Edge Node)

Software stack for a DRB (Discord Radio Bot) edge node. Runs on the SDR hardware machine. Decodes P25/DMR/NBFM radio with OP25, routes decoded audio through PulseAudio for low-latency Discord streaming and call recording, and secondarily streams to Icecast for browser listening.

## Audio Architecture

**PulseAudio is the primary audio path. Icecast is for browser listening only.**

```
RTL-SDR dongle
    │
    ▼
op25-container  ←── GnuRadio + OP25 + Liquidsoap
    │  decodes P25/DMR/NBFM
    │  HTTP API on port 8001  (start/stop/generate-config)
    │  HTTP terminal on port 8081  (live talkgroup metadata)
    │
    ├──► PulseAudio (drb_sink null sink)          ← PRIMARY audio path
    │        ~250ms latency, synchronized with OP25 call events
    │        shared via named Docker volume (pulse_socket:/run/pulse)
    │
    └──► Icecast on port 8000                     ← BROWSER STREAMING ONLY
             serves http://localhost:8000/radio
             2-5s buffering lag — NOT suitable for recording or Discord
```

```
PulseAudio (drb_sink.monitor — default source)
    │
    ├──► drb-edge-node: FFmpeg call recorder
    │        records each call to MP3, uploads to C2 server
    │        aligned with OP25 call_start / call_end events
    │
    └──► drb-edge-node: discord.py bot
             streams live audio into Discord voice channel
             continuous stream while bot is in channel
```

> **Why not Icecast for recording/Discord?**
> Icecast introduces 2-5 seconds of pipeline buffering that grows over time. Since
> call_start and call_end events fire in real-time from OP25 RF detection, using
> Icecast as the recording source causes recordings to miss the beginning and end
> of transmissions — especially on short calls (< 8s). PulseAudio has ~250ms
> latency and stays locked to the live audio position.

## System Overview

An edge node is a machine (typically a small Linux box or Raspberry Pi) with an RTL-SDR dongle attached. It connects to the central DRB C2 server over MQTT and waits for a radio system config. Once configured, it decodes transmissions and streams audio. When a Discord user runs `/join`, the server sends the node a `discord_join` command and the node puts a Discord bot in the user's voice channel that plays the live PulseAudio stream.

```
drb-edge-node  ←── FastAPI
    │  polls OP25 terminal every 0.5s — detects call start/end events
    │  publishes call metadata + status to C2 server over MQTT
    │  records each call with FFmpeg from PulseAudio, uploads to C2
    │  receives discord_join/leave commands over MQTT
    │  starts Discord bot (discord.py), joins voice channel
    │  streams PulseAudio continuously into Discord while bot is connected
    │  serves local web dashboard on port 8080
    │
    ▼
C2 Server  (remote — over MQTT + HTTP)
    stores status, metadata, recordings in Firestore/GCS
    sends commands: discord_join, discord_leave, op25_restart
    pushes system configs on admin assignment
```

## Services

| Service | Container | Description |
|---|---|---|
| `op25` | OP25 + GnuRadio (Debian) | SDR decoder — tunes to a radio system, decodes P25/DMR/NBFM, outputs audio to PulseAudio (primary) and Icecast (browser) via Liquidsoap |
| `edge-node` | Python 3.11 / FastAPI | Node agent — bridges OP25 metadata, C2 MQTT commands, call recording from PulseAudio, and Discord voice streaming from PulseAudio |
| `icecast` | Icecast2 (Debian) | Browser audio streaming only — serves the stream at `http://<node>:8000/radio` for local/remote listening. Not used for recording or Discord. |

All services run with `network_mode: host` so they share the host network and reach each other on `localhost` without any port mapping.

## Directory Structure

```
Client/
├── docker-compose.yml              # Orchestrates icecast + op25 + edge-node
├── .env.example                    # Environment variable template
├── Makefile                        # setup / test / up / down / logs
│
├── drb-edge-node/                  # FastAPI node agent
│   ├── app/
│   │   ├── main.py                 # Lifespan: wires all callbacks, starts/stops services
│   │   ├── config.py               # Pydantic settings — loaded from .env
│   │   ├── models.py               # NodeConfig, SystemConfig, CallEvent, etc.
│   │   ├── routers/
│   │   │   ├── api.py              # /api/* REST endpoints (status, op25, discord control)
│   │   │   └── ui.py               # GET / → serves index.html
│   │   ├── templates/
│   │   │   └── index.html          # Local web dashboard (dark theme, auto-refreshes every 3s)
│   │   └── internal/
│   │       ├── mqtt_manager.py     # MQTT: checkin/status/metadata pub + command/config/apikey sub
│   │       ├── metadata_watcher.py # Polls OP25 terminal at 0.5s — fires on_call_start/on_call_end
│   │       ├── op25_client.py      # HTTP client to op25-container API (start/stop/status/config)
│   │       ├── discord_radio.py    # discord.py bot — joins voice, streams PulseAudio, @mention commands
│   │       ├── call_recorder.py    # FFmpeg recording from PulseAudio per call, uploads to C2
│   │       ├── credentials.py      # Persists C2-issued API key to /configs/credentials.json
│   │       ├── config_manager.py   # Reads/writes /configs/node_config.json
│   │       └── logger.py           # Stdout logging setup
│   ├── tests/
│   │   ├── test_config_manager.py
│   │   └── test_metadata_watcher.py
│   ├── Dockerfile                  # Python 3.11-slim + ffmpeg + libopus
│   └── requirements.txt
│
├── op25-container/                 # OP25 SDR decoder service
│   ├── app/
│   │   ├── main.py                 # FastAPI + Liquidsoap config generation on startup
│   │   ├── routers/
│   │   │   └── op25_controller.py  # /op25/start /op25/stop /op25/status /op25/config
│   │   └── internal/
│   │       ├── op25_config_utils.py     # Generates active.cfg.json for OP25's multi_rx
│   │       ├── op25_liq_template.py     # Liquidsoap script template
│   │       └── liquidsoap_config_utils.py  # Renders op25.liq with Icecast credentials
│   ├── Dockerfile                  # GnuRadio + OP25 (boatbod gr310) + Liquidsoap — builds ~15min
│   ├── run_multi-rx_service.sh     # Launches OP25's multi_rx.py + HTTP terminal on port 8081
│   └── docker-entrypoint.sh       # Starts uvicorn (CRLF-safe wrapper)
│
└── icecast/                        # Icecast2 audio streaming server
    ├── Dockerfile                  # debian:bookworm-slim + icecast2
    ├── icecast.xml.template        # Config template — env vars injected at runtime
    └── entrypoint.sh               # envsubst → icecast2 as dedicated icecast user
```

## Prerequisites

- Docker + Docker Compose
- An RTL-SDR (or compatible SDR dongle) connected to the host via USB
- Network access to the DRB C2 server (MQTT port 1883 + HTTP API)
- A node ID — any unique string to identify this machine in the server dashboard

## Setup

```bash
# 1. Copy env template
cp .env.example .env

# 2. Fill in at minimum: NODE_ID and MQTT_BROKER
nano .env

# 3. Build all images (op25 takes ~10-15 minutes first time)
docker compose build

# 4. Start
docker compose up -d
```

The node will appear as **pending** in the server admin dashboard. An admin must approve it before it becomes operational. After approval, assign a radio system in the dashboard and the node will start decoding automatically.

## Environment Variables (`.env`)

| Variable | Required | Default | Description |
|---|---|---|---|
| `NODE_ID` | Yes | — | Unique ID for this node (e.g. `node-east-01`). Used in MQTT topics, Firestore docs, and the dashboard. |
| `NODE_NAME` | No | `My Radio Node` | Human-readable name shown in the dashboard and Discord `/status` |
| `NODE_LAT` | No | `0.0` | GPS latitude (decimal degrees) — shown on the server's map view |
| `NODE_LON` | No | `0.0` | GPS longitude (decimal degrees) |
| `MQTT_BROKER` | Yes | `localhost` | IP or hostname of the C2 server's MQTT broker |
| `MQTT_PORT` | No | `1883` | MQTT port |
| `MQTT_USER` | No | — | MQTT username (if the broker requires authentication) |
| `MQTT_PASS` | No | — | MQTT password |
| `C2_URL` | No | — | C2 server HTTP API URL — required for audio upload (e.g. `http://192.168.1.10:8000`) |
| `ICECAST_HOST` | No | `localhost` | Icecast hostname (leave as localhost — host network mode) |
| `ICECAST_PORT` | No | `8000` | Icecast HTTP port |
| `ICECAST_MOUNT` | No | `/radio` | Icecast mount point |
| `ICECAST_SOURCE_PASSWORD` | No | `hackme` | Icecast source password — change this |
| `ICECAST_ADMIN_PASSWORD` | No | `hackme` | Icecast admin password — change this |
| `OP25_API_URL` | No | `http://localhost:8001` | OP25 container HTTP API |
| `OP25_TERMINAL_URL` | No | `http://localhost:8081` | OP25 HTTP terminal (live talkgroup metadata) |

## Node Provisioning Flow

1. Stack starts → edge-node connects to MQTT and sends a **checkin** with its ID, name, and coordinates
2. C2 server registers the node as **pending approval** in Firestore
3. Admin clicks **Approve** in the web dashboard → C2 generates a random API key and publishes it to the node over MQTT
4. Edge node saves the API key to `/configs/credentials.json` (persists across restarts)
5. Admin assigns a **radio system** → C2 pushes the full system config over MQTT (frequencies, talkgroups, Icecast settings)
6. Edge node translates the config to OP25 format, writes `/configs/active.cfg.json`, and (re)starts OP25
7. OP25 begins decoding and streaming audio to Icecast
8. After any container restart, OP25 resumes automatically if a config is already saved

## Radio System Config

Radio system configs are created in the server dashboard (`/systems`). A system has:

- **Type**: `P25`, `DMR`, or `NBFM`
- **Control channels**: List of frequencies in Hz or MHz
- **Talkgroups**: List of `{id, name}` pairs

The C2 server translates these into OP25's `active.cfg.json` format (with Hz frequencies, decimal talkgroup IDs, and a whitelist) and pushes the config to the node over MQTT. Only talkgroups in the list are decoded and streamed.

## Discord Voice

**How it works:**

1. A Discord user runs `/join` in the server bot, picking a radio system (and optionally a specific bot token from the pool).
2. Server sends a `discord_join` command to c2-core with the target guild ID, channel ID, and an optional preferred token.
3. c2-core picks a free token from the pool, marks it `in_use`, and forwards everything to the edge node over MQTT.
4. Edge node starts a `discord.py` bot with that token and connects it to the voice channel.
5. The bot immediately begins streaming from PulseAudio (`drb_sink.monitor`) — continuous stream while connected.
6. On `/leave`, the bot disconnects and the token is released back to the pool.

**Audio source:** PulseAudio (`-f pulse "default"` → `drb_sink.monitor`). ~250ms latency, in sync with live RF. Icecast is **not** used for Discord streaming.

**Direct bot commands:**

Once a radio bot is in a voice channel, mention it in any text channel:

- **@botname joinme** — Move the bot to your current voice channel
- **@botname leave** — Disconnect the bot from voice

> The `message_content` privileged intent must be enabled for each bot token in the [Discord Developer Portal](https://discord.com/developers/applications). Without it, the bot cannot read message text and mention commands won't work.

## Local Web Dashboard

The edge node serves a dynamic dual-mode dashboard at `http://<node-ip>/`.

For complete details on both the Network Configuration Dashboard and the offline/portable Scanner UI (including hardware button mapping), please read the [UI & Hardware Guide](docs/UI_GUIDE.md).

## Volume Mounts

| Host path | Container path | Contents |
|---|---|---|
| `./configs` | `/configs` | `node_config.json` (system assignment + configured flag), `credentials.json` (C2-issued API key), `active.cfg.json` (current OP25 config) |
| `./recordings` | `/recordings` | Per-call MP3 files (held until uploaded to C2, then kept as local backup) |

The `configs` volume is critical — without it, the node forgets its API key and system config on every restart.

## Call Recording

Every call is recorded via FFmpeg reading from PulseAudio (`drb_sink.monitor`, the default source). Recordings are:
- Saved to `/recordings/YYYYMMDD_HHMMSS_<call_id>.mp3`
- Uploaded to the C2 server (if `C2_URL` is set) using the node's API key as a Bearer token
- Capped at 600 seconds for safety

**Audio source:** PulseAudio (`-f pulse -i default`). Recording starts the moment `call_start` fires from OP25, capturing the full transmission including short calls (< 8s). Icecast is **not** used for recording — its 2-5s buffering lag causes recordings to miss the start of calls and produce empty files for short transmissions.

## Makefile Targets

```
make setup    — copy .env.example → .env
make test     — run pytest inside the edge-node container
make up       — docker compose up -d
make down     — docker compose down
make logs     — follow edge-node logs
```

## Logs

```bash
docker compose logs -f edge-node    # node agent, MQTT events, call detection, Discord
docker compose logs -f op25         # SDR decoder, Liquidsoap stream
docker compose logs -f icecast      # audio streaming server
```
