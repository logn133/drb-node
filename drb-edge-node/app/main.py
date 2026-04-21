import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.config import settings
from app.models import SystemConfig
from app.internal.logger import logger
from app.internal.mqtt_manager import mqtt_manager
from app.internal import credentials
from app.internal.metadata_watcher import metadata_watcher
from app.internal.call_recorder import call_recorder
from app.internal.discord_radio import radio_bot
from app.internal.config_manager import (
    load_node_config,
    save_node_config,
)
from app.routers import api, ui


# ---------------------------------------------------------------------------
# Event handlers wired up at startup
# ---------------------------------------------------------------------------

async def on_call_start(data: dict):
    radio_bot.start_stream()
    await mqtt_manager.publish_status("recording")
    await mqtt_manager.publish_metadata("call_start", data)
    await call_recorder.start_recording(data["call_id"])


async def on_call_end(data: dict):
    radio_bot.stop_stream()
    file_path = await call_recorder.stop_recording()
    if file_path:
        node_cfg = load_node_config()
        audio_url = await call_recorder.upload_recording(
            file_path,
            data["call_id"],
            talkgroup_id=data.get("tgid"),
            talkgroup_name=data.get("tgid_name"),
            system_id=node_cfg.assigned_system_id,
        )
        if audio_url:
            data["audio_url"] = audio_url
        else:
            logger.error(f"Audio upload failed for call {data['call_id']}. Verify C2_URL and Node API Key.")
    else:
        logger.warning(
            f"No recording file generated for call {data['call_id']} "
            "— call may have been too short or Icecast unreachable."
        )
    await mqtt_manager.publish_metadata("call_end", data)
    await mqtt_manager.publish_status("online")


async def on_command(payload: dict):
    action = payload.get("action")
    logger.info(f"Command received: {action}")

    if action == "discord_join":
        token = payload.get("token")
        if not token:
            logger.error("discord_join command missing token — ignoring.")
            return
        await radio_bot.join(
            guild_id=int(payload["guild_id"]),
            channel_id=int(payload["channel_id"]),
            token=token,
            call_active=metadata_watcher.is_active,
        )
    elif action == "discord_leave":
        await radio_bot.leave()
    elif action == "op25_restart":
        from app.internal.op25_client import op25_client
        await op25_client.stop()
        await asyncio.sleep(2)
        await op25_client.start()
    elif action == "node_update":
        # TODO: Full OTA update — register a host-level systemd service (e.g. drb-update.service)
        # that stops all DRB containers, runs `docker compose pull`, then `docker compose up -d`.
        # The C2 server triggers it by sending this MQTT command; the host service watches for the
        # restart signal (e.g. via a Unix socket, a sentinel file, or a lightweight webhook).
        # Not implemented yet — for now, just restart the container so any pre-pulled image
        # is picked up (requires a prior `docker compose pull` on the host).
        logger.info("Node update requested — restarting container to pick up latest image.")
        await mqtt_manager.publish_status("offline")
        await asyncio.sleep(1)
        import os
        os._exit(0)  # Docker restart=unless-stopped will bring the container back up
    else:
        logger.warning(f"Unknown command: {action}")


async def on_api_key(payload: dict):
    key = payload.get("api_key")
    if key:
        credentials.save_api_key(key)
        logger.info("Node API key received and saved.")


def _to_hz(freq) -> int:
    """Convert a frequency to Hz. Accepts MHz floats (< 1e6) or Hz ints."""
    f = float(freq)
    return int(f * 1_000_000) if f < 1_000_000 else int(f)


async def _generate_op25_config(config: SystemConfig) -> bool:
    """Translate a SystemConfig (Firestore format) into OP25 active.cfg.json + op25.liq."""
    from app.internal.op25_client import op25_client
    raw = config.config
    payload = {
        "type": config.type,
        "systemName": config.name,
        "channels": [_to_hz(ch) for ch in raw.get("control_channels", [])],
        "tags": [
            {"talkgroup": str(tg.get("name", "")), "tagDec": int(tg["id"])}
            for tg in raw.get("talkgroups", [])
            if tg.get("id") is not None
        ],
        "whitelist": [int(tg["id"]) for tg in raw.get("talkgroups", []) if tg.get("id") is not None],
        "icecastConfig": {
            "icecast_host": settings.icecast_host,
            "icecast_port": settings.icecast_port,
            "icecast_mountpoint": settings.icecast_mount,
            "icecast_password": settings.icecast_source_password,
        },
    }
    return await op25_client.generate_config(payload)


async def on_config_push(payload: dict):
    """C2 pushes a system config — translate it to OP25 format and restart OP25."""
    try:
        config = SystemConfig(**payload)
    except Exception as e:
        logger.error(f"Invalid config push payload: {e}")
        return

    node_cfg = load_node_config()
    node_cfg.assigned_system_id = config.system_id
    node_cfg.system_config = config
    node_cfg.configured = True
    save_node_config(node_cfg)

    from app.internal.op25_client import op25_client
    if not await _generate_op25_config(config):
        logger.error(f"Failed to generate OP25 config for {config.name}")
        return

    await op25_client.stop()
    await asyncio.sleep(2)
    await op25_client.start()
    logger.info(f"Config push applied: {config.name}")


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Edge node starting — ID: {settings.node_id}")

    # Load persisted credentials (API key provisioned by C2 after approval)
    credentials.load()

    # Wire callbacks
    metadata_watcher.on_call_start = on_call_start
    metadata_watcher.on_call_end = on_call_end
    mqtt_manager.on_command = on_command
    mqtt_manager.on_config_push = on_config_push
    mqtt_manager.on_api_key = on_api_key

    # Start services (radio_bot starts on-demand when a discord_join command arrives)
    await mqtt_manager.connect()
    await metadata_watcher.start()
    await call_recorder.start()  # persistent Icecast stream buffer

    # Report initial status and resume OP25 if node was already configured before this restart
    node_cfg = load_node_config()
    initial_status = "online" if node_cfg.configured else "unconfigured"
    await mqtt_manager.publish_status(initial_status)

    if node_cfg.configured and node_cfg.system_config:
        from app.internal.op25_client import op25_client
        logger.info("Node is configured — waiting for OP25 API then generating config.")
        for attempt in range(10):
            if await _generate_op25_config(node_cfg.system_config):
                await op25_client.start()
                break
            logger.warning(f"OP25 not ready yet (attempt {attempt + 1}/10), retrying in 3s…")
            await asyncio.sleep(3)

    heartbeat_task = asyncio.create_task(mqtt_manager.heartbeat_loop())

    yield  # --- app running ---

    logger.info("Edge node shutting down.")
    heartbeat_task.cancel()
    await metadata_watcher.stop()
    await call_recorder.stop()
    await radio_bot.stop()
    await mqtt_manager.disconnect()


app = FastAPI(title=f"DRB Edge Node — {settings.node_id}", lifespan=lifespan)
app.include_router(api.router)
app.include_router(ui.router)
