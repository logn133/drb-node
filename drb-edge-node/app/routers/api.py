from fastapi import APIRouter, Depends, HTTPException, Body
from pydantic import BaseModel
from typing import Dict, List, Literal, Optional
import asyncio
import httpx
from app.config import settings
from app.models import SystemConfig, SECONDARY_SDR_MODES
from app.internal.op25_client import op25_client
from app.internal.config_manager import load_node_config, save_node_config
from app.internal.call_recorder import call_recorder
from app.internal.discord_radio import radio_bot
from app.internal.metadata_watcher import metadata_watcher
from app.internal import credentials
from app.internal.mqtt_manager import mqtt_manager
from app.internal import auth

# Every route in this router requires auth — a valid dashboard session cookie
# or HTTP Basic (see app/internal/auth.py). No exemption exists for any route
# here: there is no health/liveness endpoint in this file or anywhere else in
# the edge node (confirmed against source — no docker healthcheck references
# one either), so nothing needs to stay open for a container healthcheck.
router = APIRouter(prefix="/api", tags=["api"], dependencies=[Depends(auth.require_auth)])


@router.get("/status")
async def get_status():
    node_cfg = load_node_config()
    op25_status = await op25_client.status()

    active_tgid = metadata_watcher.current_tgid
    active_tgid_name = metadata_watcher.current_tgid_name
    system_name = None

    active_config = (
        node_cfg.override_config
        if (node_cfg.override_system_id and node_cfg.override_config)
        else node_cfg.system_config
    )
    system_type = None
    system_kind = None
    if active_config:
        from app.internal.radio_config import system_kind as _kind
        system_name = active_config.name
        system_type = active_config.type
        system_kind = _kind(active_config.type, active_config.config)
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
        "system_type": system_type,
        # Human label that tells P25 trunked from P25 conventional (node-26#24).
        "system_kind": system_kind,
        # True when what OP25 is tuned to was chosen on this dashboard rather
        # than assigned by C2 (a local override, revertible to C2's system).
        "local_choice": bool(node_cfg.override_system_id and node_cfg.override_config),
        "has_c2_system": node_cfg.system_config is not None,
        "is_recording": call_recorder.is_recording,
        # Health of the PulseAudio capture that feeds every recording — the single
        # most useful signal when recordings come back empty. Segment boundaries
        # come from this stream, so audio_silence_seconds is also how far the
        # node currently is from closing whatever it is recording.
        "audio_capture": call_recorder.is_capturing,
        "buffered_seconds": round(call_recorder.buffered_seconds, 1),
        "audio_silence_seconds": round(call_recorder.audio_activity().silence_seconds, 1),
        # Audio that was recorded but had no OP25 talkgroup anywhere near it, so
        # it was discarded rather than uploaded. Non-zero means either the
        # console is not decoding or something else is feeding drb_sink.
        "unattributed_segments": metadata_watcher.unattributed_segments,
        # Recordings whose upload to C2 failed and are waiting for a retry
        # (node-26#44), plus the outcome of the most recent upload attempt.
        "upload_spool": call_recorder.spool.stats(),
        "active_tgid": active_tgid,
        "active_tgid_name": active_tgid_name,
        "active_call_id": metadata_watcher.active_call_id,
        "discord_connected": radio_bot.is_connected,
        "icecast_url": (
            f"http://{settings.icecast_host}:{settings.icecast_port}{settings.icecast_mount}"
        ),
        # The browser builds its own URL from these + window.location.hostname;
        # icecast_host is "localhost" from the node's point of view.
        "icecast_port": settings.icecast_port,
        "icecast_mount": settings.icecast_mount,
        "op25": op25_status,
    }


@router.get("/health")
async def get_health():
    """Plain-language setup/status checklist for the dashboard (see internal/health.py)."""
    from app.internal import health
    facts = await health.gather_facts()
    return {"checks": health.build_checks(facts), "last_heard": facts["console"].get("last_heard")}


@router.post("/op25/start")
async def start_op25():
    err = await op25_client.start_with_reason()
    if err:
        raise HTTPException(502, err)
    return {"ok": True}


@router.post("/op25/stop")
async def stop_op25():
    ok = await op25_client.stop()
    if not ok:
        raise HTTPException(502, "The op25 container did not answer the stop request.")
    return {"ok": True}


@router.get("/op25/log")
async def op25_log(lines: int = 80):
    out = await op25_client.log(lines)
    if out is None:
        raise HTTPException(502, "The op25 container isn't answering.")
    return {"lines": out}


@router.get("/config")
async def get_config():
    return load_node_config()


async def _restart_op25_with(config: SystemConfig) -> None:
    """Generate OP25's config for `config` and restart OP25 on it."""
    from app.main import _generate_op25_config
    if not await _generate_op25_config(config):
        raise HTTPException(
            502, f"The op25 container did not accept the config for {config.name} — is it running?",
        )
    await op25_client.stop()
    await asyncio.sleep(2)
    err = await op25_client.start_with_reason()
    if err:
        raise HTTPException(502, f"Config saved, but OP25 did not start: {err}")


async def _apply_override(config: SystemConfig) -> None:
    """Make `config` what this node listens to, chosen locally.

    Stored as an override so a C2-assigned system (if any) survives and can
    be reverted to, and so C2 sees the node is overridden. A later C2 config
    push replaces it.
    """
    node_cfg = load_node_config()
    node_cfg.override_system_id = config.system_id
    node_cfg.override_config = config
    save_node_config(node_cfg)
    await _restart_op25_with(config)
    if mqtt_manager.is_connected:
        await mqtt_manager.publish_checkin()


@router.post("/config/system")
async def set_system_config(config: SystemConfig):
    """
    Assign a system as if C2 had (persists as the node's own system, not an
    override) and restart OP25 on it.

    Used to write the raw `config` blob straight to active.cfg.json, which is
    not an OP25 config at all — OP25 could not start from it.
    """
    _validate_type(config.type)
    node_cfg = load_node_config()
    node_cfg.assigned_system_id = config.system_id
    node_cfg.system_config = config
    node_cfg.configured = True
    save_node_config(node_cfg)
    await _restart_op25_with(config)
    return {"ok": True}


def _validate_type(system_type: str) -> None:
    if str(system_type).upper() == "DMR":
        raise HTTPException(400, "DMR isn't supported by this node's decoder yet — use P25 or analog (NBFM).")
    if str(system_type).upper() not in ("P25", "NBFM"):
        raise HTTPException(400, f"Unknown system type {system_type!r} — use P25 or NBFM.")


class Talkgroup(BaseModel):
    id: int
    name: str = ""


class ListenBody(BaseModel):
    """What to listen to, chosen on the local dashboard."""
    mode: Literal["system", "p25", "p25conv", "analog"]
    system_id: Optional[str] = None       # mode=system: a cached C2 system
    name: Optional[str] = None
    # MHz. p25 = control channels; p25conv = 1-2 voice channels (node-26#24);
    # analog = exactly one.
    frequencies: List[float] = []
    talkgroups: List[Talkgroup] = []       # p25 only; empty = every talkgroup
    nac: Optional[str] = None              # p25conv only, hex; empty = any NAC


# RTL-SDR (R820T/R828D) tuning range.
_MIN_MHZ, _MAX_MHZ = 24.0, 1766.0


@router.post("/listen")
async def listen(body: ListenBody):
    """Point OP25 at a system or frequency and start it — no C2 needed."""
    if body.mode == "system":
        from app.internal.system_cacher import get_cached_system
        cached = get_cached_system(body.system_id or "")
        if not cached:
            raise HTTPException(404, "That system isn't in this node's saved list.")
        config = SystemConfig(**cached)
        _validate_type(config.type)
    else:
        freqs = body.frequencies
        if not freqs:
            raise HTTPException(400, "Enter at least one frequency in MHz.")
        bad = [f for f in freqs if not (_MIN_MHZ <= f <= _MAX_MHZ)]
        if bad:
            raise HTTPException(
                400, f"{bad[0]:g} isn't a frequency an RTL-SDR can tune — enter MHz between "
                f"{_MIN_MHZ:g} and {_MAX_MHZ:g} (e.g. 851.0125).",
            )
        if body.mode == "analog" and len(freqs) != 1:
            raise HTTPException(400, "Analog listens to exactly one frequency.")
        first = f"{freqs[0]:.5f}".rstrip("0").rstrip(".")
        if body.mode == "p25conv":
            config = _conventional_listen_config(body, freqs, first)
        else:
            is_p25 = body.mode == "p25"
            label = (body.name or "").strip() or (f"P25 {first} MHz" if is_p25 else f"Analog {first} MHz")
            config = SystemConfig(
                system_id=f"local-{body.mode}",
                name=label,
                type="P25" if is_p25 else "NBFM",
                config={
                    "control_channels": freqs,
                    "talkgroups": [tg.model_dump() for tg in body.talkgroups] if is_p25 else [],
                },
            )
    await _apply_override(config)
    return {"ok": True, "name": config.name}


# One SDR's usable passband, less a channel's width — op25-container makes the
# exact check; this one just turns the common mistake into a readable error.
_CONVENTIONAL_MAX_SPAN_MHZ = 1.5


def _conventional_listen_config(body: ListenBody, freqs: List[float], first: str) -> SystemConfig:
    from app.internal.radio_config import MAX_CONVENTIONAL_CHANNELS, parse_nac
    if len(freqs) > MAX_CONVENTIONAL_CHANNELS:
        raise HTTPException(400, f"P25 conventional listens to at most {MAX_CONVENTIONAL_CHANNELS} channels.")
    if len(set(freqs)) != len(freqs):
        raise HTTPException(400, "The same frequency is entered twice.")
    if max(freqs) - min(freqs) > _CONVENTIONAL_MAX_SPAN_MHZ:
        raise HTTPException(
            400, f"Those channels are {max(freqs) - min(freqs):.3f} MHz apart — one SDR covers about "
            f"{_CONVENTIONAL_MAX_SPAN_MHZ:g} MHz. Pick channels closer together.",
        )
    try:
        nac = parse_nac(body.nac)
    except ValueError as e:
        raise HTTPException(400, str(e))
    label = (body.name or "").strip() or f"P25 conventional {first} MHz"
    return SystemConfig(
        system_id="local-p25conv",
        name=label,
        type="P25",
        config={
            "mode": "conventional",
            "channels": [
                {"freq": f, "name": f"{label} {f:.4f}" if len(freqs) > 1 else label,
                 **({"nac": f"{nac:03X}"} if nac is not None else {})}
                for f in freqs
            ],
            "talkgroups": [],
        },
    )


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

    _validate_type(config.type)
    await _apply_override(config)
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
        await _restart_op25_with(node_cfg.system_config)
    else:
        # Nothing from C2 to go back to: stop rather than keep decoding a
        # system the dashboard now says is cleared.
        await op25_client.stop()

    if mqtt_manager.is_connected:
        await mqtt_manager.publish_checkin()
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


@router.get("/sdr")
async def get_sdr():
    """Which SDR does what: detected dongles, pins, priority, what's running."""
    from app.internal.secondary_sdr_client import secondary_sdr_client
    from app.internal.sdr_settings import op25_serial
    cfg = load_node_config()
    devs = await secondary_sdr_client.devices()
    status = await secondary_sdr_client.status()
    return {
        "devices": devs,  # None = secondary-sdr service unreachable
        "op25_serial": op25_serial(cfg, devs),
        "pins": cfg.sdr_pins,
        "priority": cfg.secondary_sdr_priority,
        "modes": list(SECONDARY_SDR_MODES),
        "running": [r["mode"] for r in status.get("running", [])] if status else None,
    }


class SdrSettingsBody(BaseModel):
    priority: Optional[List[str]] = None
    pins: Optional[Dict[str, Optional[str]]] = None  # service -> serial; null/"" = automatic


@router.post("/sdr")
async def set_sdr(body: SdrSettingsBody):
    """Set priority and/or pins. Restarts OP25 only if OP25's own SDR changes."""
    from app.internal.sdr_settings import set_sdr_settings
    if body.priority is not None:
        unknown = [m for m in body.priority if m not in SECONDARY_SDR_MODES]
        if unknown:
            raise HTTPException(400, f"Unknown secondary SDR mode(s): {unknown}")
    try:
        running = await set_sdr_settings(body.priority, body.pins)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "running": running}


@router.get("/discord")
async def discord_status():
    """What the node's radio bots are doing (desired state from C2, actual here)."""
    return radio_bot.snapshot()


@router.post("/discord/join")
async def discord_join():
    # node-26#25 item 4: this used to call join() without a token and 500 every
    # time. A node cannot lease a radio bot by itself — C2 owns the pool.
    raise HTTPException(
        409,
        "Discord radio channels are started from Discord (/join) or the DRB web app; "
        "the node cannot pick a radio bot on its own.",
    )


@router.post("/discord/leave")
async def discord_leave():
    """Local stop: log every radio bot out until DRB sends a new channel list."""
    await radio_bot.pause_locally()
    return {"ok": True}
