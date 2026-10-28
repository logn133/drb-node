"""
Which SDR does what on this node (node-26#9, node-26#11).

- OP25 always has exactly one dongle. `sdr_pins["op25"]` names it by serial;
  unpinned, OP25 keeps its historical "first dongle" — but that dongle is now
  named by serial too, so the decoders can never take it out from under OP25.
- Every other dongle runs the next enabled service in `secondary_sdr_priority`.
  `sdr_pins[mode]` optionally binds a service to the dongle carrying its
  antenna; unpinned services take any spare.

One apply path for the local dashboard, C2 commands and config pushes.
"""
import asyncio
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from app.config import settings
from app.internal.config_manager import load_node_config, save_node_config
from app.internal.logger import logger
from app.internal.secondary_sdr_client import secondary_sdr_client
from app.models import NodeConfig, normalize_sdr_pins, normalize_secondary_priority

_OP25_CONFIG = Path(settings.config_path) / "active.cfg.json"


def op25_serial(cfg: NodeConfig, devs: Optional[List[Dict[str, Any]]]) -> Optional[str]:
    """The serial of OP25's dongle: the pin, else the first detected dongle
    (what OP25's plain "rtl" device string has always opened)."""
    if cfg.sdr_pins.get("op25"):
        return cfg.sdr_pins["op25"]
    return devs[0]["serial"] if devs else None


async def pin_op25_device() -> Optional[str]:
    """Rewrite OP25's generated config to open its dongle by serial.

    Runs after every generate-config (op25_client.generate_config). A plain
    "rtl" means "device 0", and which dongle is device 0 depends on who opened
    what first — that is how OP25 lost its SDR to readsb on 2026-09-27.
    Left as "rtl" only when the serial is unknown or shared by two dongles.
    """
    devs = await secondary_sdr_client.devices()
    serial = op25_serial(load_node_config(), devs)
    if not serial or (devs and sum(d["serial"] == serial for d in devs) > 1):
        return None
    try:
        cfg = json.loads(_OP25_CONFIG.read_text())
        for dev in cfg.get("devices", []):
            dev["args"] = f"rtl={serial}"
        _OP25_CONFIG.write_text(json.dumps(cfg, indent=2))
    except Exception as e:
        logger.error(f"Could not pin OP25 to SDR {serial}: {e}")
        return None
    return serial


async def apply_secondaries() -> Optional[List[str]]:
    """Start/stop decoders to match the saved priority and pins, never on OP25's dongle."""
    cfg = load_node_config()
    if not cfg.secondary_sdr_priority:
        return await secondary_sdr_client.apply([], {}, [])
    devs = await secondary_sdr_client.devices()
    reserved = [s for s in [op25_serial(cfg, devs)] if s]
    pins = {m: s for m, s in cfg.sdr_pins.items() if m != "op25"}
    return await secondary_sdr_client.apply(cfg.secondary_sdr_priority, pins, reserved)


async def set_sdr_settings(priority: Optional[Iterable[str]] = None,
                           pins: Optional[Dict[str, Optional[str]]] = None) -> Optional[List[str]]:
    """Persist and apply priority and/or pins. OP25 restarts only if its own
    dongle changed; reordering the spare dongles never interrupts P25.

    Returns the decoders now running (None if the container is unreachable).
    """
    cfg = load_node_config()
    old_op25 = cfg.sdr_pins.get("op25")
    if priority is not None:
        ordered = normalize_secondary_priority(priority)
        cfg.secondary_sdr_priority = ordered
        cfg.secondary_sdr_mode = ordered[0] if ordered else "none"
    if pins is not None:
        cfg.sdr_pins = normalize_sdr_pins(pins)
    save_node_config(cfg)

    if cfg.sdr_pins.get("op25") != old_op25 and settings.recorder_backend == "trunk-recorder":
        # Trunk Recorder takes the "op25" dongle (node-26#45): rewrite its
        # config with the new serial; run.sh restarts it on the change.
        from app.internal.health import active_config
        from app.main import _generate_op25_config
        await secondary_sdr_client.apply([], {}, [])
        current = active_config(cfg)
        if current is not None and await _generate_op25_config(current):
            from app.internal import radio_backend
            await radio_backend.start()  # waits until TR holds the new dongle
        logger.info(f"Trunk Recorder moved to SDR {cfg.sdr_pins.get('op25') or 'rtl=0 (first dongle)'}")
    elif cfg.sdr_pins.get("op25") != old_op25:
        from app.internal.op25_client import op25_client
        # Free the new dongle first if a decoder holds it, then move OP25.
        await secondary_sdr_client.apply([], {}, [])
        serial = await pin_op25_device()
        await op25_client.stop()
        await asyncio.sleep(2)
        await op25_client.start()
        logger.info(f"OP25 moved to SDR {serial or 'rtl (first dongle)'}")

    running = await apply_secondaries()
    logger.info(f"SDR settings: priority={cfg.secondary_sdr_priority!r} pins={cfg.sdr_pins!r}; running {running!r}")

    # Report straight away so C2's view doesn't wait for the next heartbeat.
    from app.internal.mqtt_manager import mqtt_manager
    asyncio.create_task(mqtt_manager.publish_checkin())
    return running
