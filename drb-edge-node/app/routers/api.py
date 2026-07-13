from fastapi import APIRouter, HTTPException, Body
from typing import Optional
import asyncio
import httpx
from app.config import settings
from app.models import SystemConfig
from app.internal.op25_client import op25_client
from app.internal.config_manager import load_node_config, save_node_config, apply_system_config
from app.internal.call_recorder import call_recorder
from app.internal.discord_radio import radio_bot
from app.internal.metadata_watcher import metadata_watcher
from app.internal import credentials
from app.internal.mqtt_manager import mqtt_manager

router = APIRouter(prefix="/api", tags=["api"])


@router.get("/status")
async def get_status():
    node_cfg = load_node_config()
    op25_status = await op25_client.status()
    
    active_tgid = metadata_watcher.current_tgid
    active_tgid_name = metadata_watcher.current_tgid_name
    system_name = None
    
    active_config = node_cfg.override_config if (node_cfg.override_system_id and node_cfg.override_config) else node_cfg.system_config
    if active_config:
        system_name = active_config.name
        if active_tgid:
            # Reverse engineer the TGID to talkgroup name based on active system object
            tgs = active_config.config.get("talkgroups", [])
            for tg in tgs:
                if str(tg.get("id")) == str(active_tgid):
                    active_tgid_name = tg.get("name")
                    break

    return {
        "node_id": settings.node_id,
        "node_name": settings.node_name,
        "lat": settings.node_lat,
        "lon": settings.node_lon,
        "node_type": node_cfg.node_type,
        "enforce_override_timeout": node_cfg.enforce_override_timeout,
        "is_overridden": node_cfg.override_system_id is not None and node_cfg.node_type != "portable",
        "override_system_id": node_cfg.override_system_id,
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


@router.get("/systems")
async def get_systems():
    from app.internal.system_cacher import load_cached_systems, fetch_and_cache_systems
    asyncio.create_task(fetch_and_cache_systems())
    return load_cached_systems()


@router.post("/config/override")
async def set_override(
    system_id: Optional[str] = Body(None),
    system_config: Optional[dict] = Body(None)
):
    node_cfg = load_node_config()
    config = None
    
    if system_id:
        from app.internal.system_cacher import get_cached_system
        cached = get_cached_system(system_id)
        if not cached:
            raise HTTPException(404, f"System '{system_id}' not found in cache.")
        config = SystemConfig(**cached)
    elif system_config:
        if not all(k in system_config for k in ("system_id", "name", "type", "config")):
            raise HTTPException(400, "Invalid manual system config schema.")
        config = SystemConfig(**system_config)
    else:
        raise HTTPException(400, "Must specify system_id or system_config.")
        
    node_cfg.override_system_id = config.system_id
    node_cfg.override_config = config
    save_node_config(node_cfg)
    
    from app.main import _generate_op25_config
    if not await _generate_op25_config(config):
        raise HTTPException(500, f"Failed to generate OP25 config for override: {config.name}")
        
    await op25_client.stop()
    await asyncio.sleep(2)
    await op25_client.start()
    
    await mqtt_manager._publish_checkin()
    return {"ok": True}


@router.post("/config/revert")
async def revert_config():
    node_cfg = load_node_config()
    if not node_cfg.override_system_id:
        return {"ok": True, "message": "No override active."}
        
    node_cfg.override_system_id = None
    node_cfg.override_config = None
    save_node_config(node_cfg)
    
    if node_cfg.system_config:
        from app.main import _generate_op25_config
        if not await _generate_op25_config(node_cfg.system_config):
            raise HTTPException(500, "Failed to regenerate original OP25 config.")
            
        await op25_client.stop()
        await asyncio.sleep(2)
        await op25_client.start()
        
    await mqtt_manager._publish_checkin()
    return {"ok": True}


@router.post("/config/override/ack")
async def ack_override(timeout_minutes: int = Body(1440)):
    if not settings.c2_url:
        raise HTTPException(400, "C2_URL not configured.")
        
    api_key = credentials.get_api_key()
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{settings.c2_url}/nodes/{settings.node_id}/override/ack",
                json={"timeout_minutes": timeout_minutes},
                headers=headers
            )
            r.raise_for_status()
            return r.json()
    except Exception as e:
        raise HTTPException(500, f"Failed to contact C2: {e}")


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
