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
    apply_system_config,
)
from app.routers import api, ui


# ---------------------------------------------------------------------------
# Event handlers wired up at startup
# ---------------------------------------------------------------------------

async def on_call_start(data: dict):
    await mqtt_manager.publish_status("recording")
    await mqtt_manager.publish_metadata("call_start", data)
    await call_recorder.start_recording(data["call_id"])


async def on_call_end(data: dict):
    file_path = await call_recorder.stop_recording()
    if file_path:
        audio_url = await call_recorder.upload_recording(file_path, data["call_id"])
        if audio_url:
            data["audio_url"] = audio_url
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
        )
    elif action == "discord_leave":
        await radio_bot.leave()
    elif action == "op25_restart":
        from app.internal.op25_client import op25_client
        await op25_client.stop()
        await asyncio.sleep(2)
        await op25_client.start()
    else:
        logger.warning(f"Unknown command: {action}")


async def on_api_key(payload: dict):
    key = payload.get("api_key")
    if key:
        credentials.save_api_key(key)
        logger.info("Node API key received and saved.")


async def on_config_push(payload: dict):
    """C2 pushes a system config — apply it and restart OP25 with the new settings."""
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
    apply_system_config(config)

    # Restart OP25 so it picks up the new config
    from app.internal.op25_client import op25_client
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

    # Report initial status and resume OP25 if node was already configured before this restart
    node_cfg = load_node_config()
    initial_status = "online" if node_cfg.configured else "unconfigured"
    await mqtt_manager.publish_status(initial_status)

    if node_cfg.configured:
        from app.internal.op25_client import op25_client
        logger.info("Node is configured — starting OP25.")
        await op25_client.start()

    heartbeat_task = asyncio.create_task(mqtt_manager.heartbeat_loop())

    yield  # --- app running ---

    logger.info("Edge node shutting down.")
    heartbeat_task.cancel()
    await metadata_watcher.stop()
    await radio_bot.stop()
    await mqtt_manager.disconnect()


app = FastAPI(title=f"DRB Edge Node — {settings.node_id}", lifespan=lifespan)
app.include_router(api.router)
app.include_router(ui.router)
