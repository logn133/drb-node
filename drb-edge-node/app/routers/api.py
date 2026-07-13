from fastapi import APIRouter, HTTPException
from app.config import settings
from app.models import SystemConfig
from app.internal.op25_client import op25_client
from app.internal.config_manager import load_node_config, save_node_config, apply_system_config
from app.internal.call_recorder import call_recorder
from app.internal.discord_radio import radio_bot
from app.internal.metadata_watcher import metadata_watcher

router = APIRouter(prefix="/api", tags=["api"])


@router.get("/status")
async def get_status():
    node_cfg = load_node_config()
    op25_status = await op25_client.status()
    
    active_tgid = metadata_watcher.current_tgid
    active_tgid_name = metadata_watcher.current_tgid_name
    system_name = None
    
    if node_cfg.system_config:
        system_name = node_cfg.system_config.name
        if active_tgid:
            # Reverse engineer the TGID to talkgroup name based on system object
            tgs = node_cfg.system_config.config.get("talkgroups", [])
            for tg in tgs:
                if str(tg.get("id")) == str(active_tgid):
                    active_tgid_name = tg.get("name")
                    break

    return {
        "node_id": settings.node_id,
        "node_name": settings.node_name,
        "lat": settings.node_lat,
        "lon": settings.node_lon,
        "configured": node_cfg.configured,
        "assigned_system_id": node_cfg.assigned_system_id,
        "system_name": system_name,
        "is_recording": call_recorder.is_recording,
        "active_tgid": active_tgid,
        "active_tgid_name": active_tgid_name,
        "active_call_id": metadata_watcher.active_call_id,
        "discord_connected": radio_bot.is_connected,
        "icecast_url": (
            f"http://{settings.icecast_host}:{settings.icecast_port}{settings.icecast_mount}"
        ),
        "op25": op25_status,
    }


@router.post("/op25/start")
async def start_op25():
    ok = await op25_client.start()
    if not ok:
        raise HTTPException(500, "Failed to start OP25")
    return {"ok": True}


@router.post("/op25/stop")
async def stop_op25():
    ok = await op25_client.stop()
    if not ok:
        raise HTTPException(500, "Failed to stop OP25")
    return {"ok": True}


@router.get("/config")
async def get_config():
    return load_node_config()


@router.post("/config/system")
async def set_system_config(config: SystemConfig):
    """
    Apply a system config locally — called by the web UI or pushed by C2.
    Writes the OP25 config and persists the node config.
    """
    node_cfg = load_node_config()
    node_cfg.assigned_system_id = config.system_id
    node_cfg.system_config = config
    node_cfg.configured = True
    save_node_config(node_cfg)
    apply_system_config(config)
    return {"ok": True}


@router.post("/discord/join")
async def discord_join(guild_id: int, channel_id: int):
    ok = await radio_bot.join(guild_id, channel_id)
    if not ok:
        raise HTTPException(500, "Failed to join voice channel")
    return {"ok": True}


@router.post("/discord/leave")
async def discord_leave():
    await radio_bot.leave()
    return {"ok": True}
