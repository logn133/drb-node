import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI
from app.config import settings
from app.models import SystemConfig, normalize_sdr_pins, normalize_secondary_priority
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

def _iso(epoch: Optional[float]) -> Optional[str]:
    """Epoch → UTC ISO-8601, matching metadata_watcher's timestamp format."""
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


# call_ids whose `call_start` has already gone out over MQTT. A segment can open
# before its talkgroup is known (audio onset can precede the OP25 grant), and
# C2's _on_call_start writes talkgroup_id straight into a new Firestore `calls`
# doc — publishing early with tgid=None would create a permanently untagged call.
# So the start is held back until attribution succeeds, and replayed just before
# the end event if it resolved late.
_published_starts: set = set()


async def on_call_start(data: dict):
    radio_bot.start_stream()
    await mqtt_manager.publish_status("recording")
    # started_at_epoch is the detected voice onset (or, in console fallback mode,
    # OP25's call_log timestamp). The recorder slices the ring buffer back to it
    # minus the pre-roll, so however late the poll loop noticed, the audio still
    # starts in the right place.
    await call_recorder.start_recording(
        data["call_id"],
        start_epoch=data.get("started_at_epoch"),
    )

    if data.get("attributed", True):
        _published_starts.add(data["call_id"])
        await mqtt_manager.publish_metadata("call_start", data)
    else:
        logger.info(
            f"Call {data['call_id']} started on audio onset with no talkgroup yet — holding the "
            "call_start event until the console attributes it."
        )


async def on_call_end(data: dict):
    radio_bot.stop_stream()
    call_id = data["call_id"]
    published_start = call_id in _published_starts
    _published_starts.discard(call_id)

    if not data.get("attributed", True):
        # ORPHAN AUDIO. metadata_watcher has already logged the details at ERROR.
        # The audio is dropped rather than uploaded: a call with no talkgroup is
        # worse than no call at all, because it silently poisons correlation.
        await call_recorder.discard_recording()
        if published_start:
            # Should not happen (attribution only ever improves), but if a start
            # did go out, the doc must not be left hanging in "active".
            data["audio_skipped"] = "unattributed"
            await mqtt_manager.publish_metadata("call_end", data)
        await mqtt_manager.publish_status("online")
        return

    recording = await call_recorder.stop_recording(end_epoch=data.get("ended_at_epoch"))

    if recording is not None and recording.path is not None:
        # Silence trimming shortens the audio, so the audio's own bounds no
        # longer equal the call's. `started_at`/`ended_at` keep meaning the CALL
        # (what OP25 observed on the control channel) — these extra fields carry
        # the AUDIO's wall-clock bounds so playback, correlation and incident
        # timelines can still map an audio offset back to real time:
        #     wall_clock_of(audio_offset_t) == audio_start_epoch + t
        data["audio_start_at"] = _iso(recording.audio_start_epoch)
        data["audio_end_at"] = _iso(recording.audio_end_epoch)
        data["audio_start_epoch"] = recording.audio_start_epoch
        data["audio_end_epoch"] = recording.audio_end_epoch
        data["audio_lead_trimmed"] = round(recording.lead_trimmed, 3)
        data["audio_tail_trimmed"] = round(recording.tail_trimmed, 3)
        if recording.clamped_seconds:
            data["audio_clamped_seconds"] = round(recording.clamped_seconds, 3)

    if recording is not None and recording.path is not None:
        node_cfg = load_node_config()
        audio_url = await call_recorder.upload_recording(
            recording.path,
            data["call_id"],
            talkgroup_id=data.get("tgid"),
            talkgroup_name=data.get("tgid_name"),
            system_id=node_cfg.assigned_system_id,
            audio_start_epoch=recording.audio_start_epoch,
            audio_end_epoch=recording.audio_end_epoch,
        )
        if audio_url:
            data["audio_url"] = audio_url
        else:
            logger.error(f"Audio upload failed for call {data['call_id']}. Verify C2_URL and Node API Key.")
    elif recording is not None and recording.all_silence:
        # Explicit policy: an all-silence recording is not uploaded. It has no
        # transcript value and silence is what makes Whisper invent text.
        data["audio_skipped"] = "all_silence"
        logger.warning(f"Call {data['call_id']} was pure silence — no upload. Investigate the audio path.")
    else:
        logger.warning(
            f"No recording file generated for call {data['call_id']} "
            "— PulseAudio capture may be down (check the op25 container and "
            f"the {settings.pulse_source} source)."
        )

    if not published_start:
        # Attribution arrived after the segment opened. Replay the start so C2
        # creates the `calls` doc with the right talkgroup before the end event
        # updates it.
        start_payload = {
            key: data[key]
            for key in ("call_id", "tgid", "tgid_name", "freq", "srcaddr",
                        "started_at", "started_at_epoch", "attributed", "driver")
            if key in data
        }
        await mqtt_manager.publish_metadata("call_start", start_payload)
    await mqtt_manager.publish_metadata("call_end", data)
    await mqtt_manager.publish_status("online")


async def on_command(payload: dict):
    action = payload.get("action")
    logger.info(f"Command received: {action}")

    if action in ("discord_join", "discord_leave"):
        # Pre-desired-state C2. Discord streams now arrive on
        # nodes/{id}/discord/desired (node-26#25); a raw-token join is refused
        # so a bot token is never taken from an MQTT command again.
        logger.warning(f"Ignoring legacy {action} command — Discord radio is driven by desired state now.")
    elif action in ("set_sdr_config", "set_secondary_priority"):
        from app.internal.sdr_settings import set_sdr_settings
        try:
            await set_sdr_settings(payload.get("priority"), payload.get("pins"))
        except ValueError as e:
            logger.error(f"Rejected SDR settings from C2: {e}")
    elif action == "op25_restart":
        from app.internal.op25_client import op25_client
        await op25_client.stop()
        await asyncio.sleep(2)
        await op25_client.start()
    elif action == "node_update":
        # node-26#15: the container cannot (and must not) update itself. It
        # drops a sentinel in /configs; the host's drb-update.path unit
        # (installed by install.sh) sees it and runs drb-update.service, which
        # pulls the node's own pinned repo ref + images and recreates the
        # stack. The sentinel's content is ignored by the host — C2 cannot
        # choose what gets installed. Offline/locked-out nodes get the same
        # request over HTTPS via the updater's timer instead.
        from app.internal.update_request import request_host_update
        request_host_update()
    else:
        logger.warning(f"Unknown command: {action}")


async def on_api_key(payload: dict):
    key = payload.get("api_key")
    if key:
        credentials.save_api_key(key)
        mqtt_manager.update_api_key(key)
        logger.info("Node API key received and saved.")


async def _generate_op25_config(config: SystemConfig) -> bool:
    """Translate a SystemConfig (Firestore format) into OP25 active.cfg.json + op25.liq.

    Trunked / conventional / analog are told apart in radio_config (node-26#24);
    a system saved before that has no `mode` and is trunked, as it always was.
    """
    from app.internal.op25_client import op25_client
    from app.internal.radio_config import build_generate_payload
    node_cfg = load_node_config()
    try:
        payload = build_generate_payload(
            config.type, config.name, config.config,
            icecast={
                "icecast_host": settings.icecast_host,
                "icecast_port": settings.icecast_port,
                "icecast_mountpoint": settings.icecast_mount,
                "icecast_password": settings.icecast_source_password,
            },
            hardware_preset=node_cfg.hardware_preset,
            ppm_override=node_cfg.ppm_override,
        )
    except ValueError as e:
        logger.error(f"System {config.name!r} has an unusable channel list: {e}")
        return False
    return await op25_client.generate_config(payload)


async def on_config_push(payload: dict):
    """C2 pushes a system config — translate it to OP25 format and restart OP25."""
    hardware_preset = payload.pop("hardware_preset", None)
    ppm_override = payload.pop("ppm_override", None)
    node_type = payload.pop("node_type", None)
    secondary_sdr_priority = payload.pop("secondary_sdr_priority", None)
    sdr_pins = payload.pop("sdr_pins", None)
    secondary_sdr_mode = payload.pop("secondary_sdr_mode", None)  # legacy single-mode C2
    enforce_override_timeout = payload.pop("enforce_override_timeout", None)
    try:
        config = SystemConfig(**payload)
    except Exception as e:
        logger.error(f"Invalid config push payload: {e}")
        return

    node_cfg = load_node_config()
    node_cfg.assigned_system_id = config.system_id
    node_cfg.system_config = config
    node_cfg.configured = True
    node_cfg.override_system_id = None
    node_cfg.override_config = None
    if hardware_preset is not None:
        node_cfg.hardware_preset = hardware_preset
    if ppm_override is not None:
        node_cfg.ppm_override = float(ppm_override)
    if node_type is not None:
        node_cfg.node_type = node_type
    if enforce_override_timeout is not None:
        node_cfg.enforce_override_timeout = bool(enforce_override_timeout)
    if secondary_sdr_priority is None and secondary_sdr_mode is not None:
        secondary_sdr_priority = [secondary_sdr_mode]
    if secondary_sdr_priority is not None:
        node_cfg.secondary_sdr_priority = normalize_secondary_priority(secondary_sdr_priority)
        node_cfg.secondary_sdr_mode = (node_cfg.secondary_sdr_priority or ["none"])[0]
    if sdr_pins is not None:
        try:
            node_cfg.sdr_pins = normalize_sdr_pins(sdr_pins)
        except ValueError as e:
            logger.error(f"Ignoring invalid sdr_pins in config push: {e}")
    save_node_config(node_cfg)

    from app.internal.op25_client import op25_client
    if not await _generate_op25_config(config):
        logger.error(f"Failed to generate OP25 config for {config.name}")
        return

    # OP25's (pinned) dongle may be one a decoder holds right now: free the
    # spares, restart OP25 on its own dongle, then hand the rest back out.
    from app.internal.secondary_sdr_client import secondary_sdr_client
    from app.internal.sdr_settings import apply_secondaries
    await secondary_sdr_client.apply([], {}, [])
    await op25_client.stop()
    await asyncio.sleep(2)
    await op25_client.start()
    logger.info(f"Config push applied: {config.name}")
    await apply_secondaries()


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Edge node starting — ID: {settings.node_id}")

    # Load persisted credentials (API key provisioned by C2 after approval;
    # also generates/loads the local dashboard's auth salt + session secret)
    credentials.load()
    from app.internal import auth
    auth.warn_if_default_password()

    # Wire callbacks
    metadata_watcher.on_call_start = on_call_start
    metadata_watcher.on_call_end = on_call_end
    # Segment boundaries come from the audio itself; this is how the watcher
    # sees it. Without this the watcher falls back to control-channel
    # segmentation, which is measurably wrong in both directions.
    metadata_watcher.audio_activity = call_recorder.audio_activity
    mqtt_manager.on_command = on_command
    mqtt_manager.on_config_push = on_config_push
    mqtt_manager.on_api_key = on_api_key
    mqtt_manager.on_discord_desired = radio_bot.apply_desired

    # Start services (radio_bot starts when C2's retained discord/desired arrives)
    await mqtt_manager.connect()
    await metadata_watcher.start()
    await call_recorder.start()  # persistent PulseAudio ring buffer

    # Start system caching in background
    from app.internal.system_cacher import fetch_and_cache_systems
    asyncio.create_task(fetch_and_cache_systems())

    # Report initial status and resume OP25 if node was already configured before this restart
    node_cfg = load_node_config()
    initial_status = "online" if node_cfg.configured else "unconfigured"
    await mqtt_manager.publish_status(initial_status)

    active_config = (
        node_cfg.override_config
        if (node_cfg.override_system_id and node_cfg.override_config)
        else node_cfg.system_config
    )
    # Not gated on `configured` (set only by a C2 assignment): a standalone or
    # not-yet-approved node listening to a system chosen on the local
    # dashboard (an override) must come back on it after a reboot too.
    if active_config:
        from app.internal.op25_client import op25_client
        logger.info("Node has a system to listen to — waiting for OP25 API then generating config.")
        for attempt in range(10):
            if await _generate_op25_config(active_config):
                await op25_client.start()
                break
            logger.warning(f"OP25 not ready yet (attempt {attempt + 1}/10), retrying in 3s…")
            await asyncio.sleep(3)

    # After op25 has claimed its dongle, so the decoders only get the spares.
    if node_cfg.secondary_sdr_priority:
        from app.internal.sdr_settings import apply_secondaries
        logger.info(f"Resuming secondary SDRs (priority={node_cfg.secondary_sdr_priority!r}) after restart.")
        await apply_secondaries()

    heartbeat_task = asyncio.create_task(mqtt_manager.heartbeat_loop())
    from app.internal.telemetry_uplink import telemetry_uplink_loop
    telemetry_task = asyncio.create_task(telemetry_uplink_loop())

    yield  # --- app running ---

    logger.info("Edge node shutting down.")
    heartbeat_task.cancel()
    telemetry_task.cancel()
    await metadata_watcher.stop()
    await call_recorder.stop()
    await radio_bot.stop()
    await mqtt_manager.disconnect()


app = FastAPI(title=f"DRB Edge Node — {settings.node_id}", lifespan=lifespan)
app.include_router(api.router)
app.include_router(ui.router)
