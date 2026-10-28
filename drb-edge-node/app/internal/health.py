"""
Plain-language health checklist for the local dashboard (/api/health).

A first-time owner opens http://<node>/ and needs three answers: is this node
working, if not what is wrong, and what do I do about it. Each check below
answers one of those for one link in the chain

    password -> C2 enrollment -> MQTT -> SDR -> what to listen to -> OP25
             -> decoding -> audio (PulseAudio) -> local stream (Icecast)

With RECORDER_BACKEND=trunk-recorder (node-26#45) the last four are replaced
by Trunk Recorder's: supervisor state, control-channel decode rate (from its
log), recordings handed over, and the live feed to Discord.

`gather_facts()` does the I/O; `build_checks()` is a pure function of those
facts so every state (unapproved, no SDR, OP25 crashed, …) is unit-testable.

Read-only: nothing here changes node state, and it never polls the OP25
terminal itself (that drains OP25's call_log, which metadata_watcher owns) —
it reads metadata_watcher.console_state instead.
"""
import time
from typing import Any, Dict, List, Optional

import httpx

from app.config import settings
from app.internal.logger import logger

OK, WARN, ERROR, OFF = "ok", "warn", "error", "off"

# OP25's console answers within a second or two of a healthy multi_rx; past
# this the console is considered silent.
CONSOLE_STALE_SECONDS = 5.0
# P25 control channels send several messages a second; this long without one
# means OP25 is not decoding.
TSBK_STALE_SECONDS = 15.0


def _check(cid: str, label: str, state: str, summary: str, action: str = "") -> Dict[str, str]:
    return {"id": cid, "label": label, "state": state, "summary": summary, "action": action}


def _mhz(freq: Any) -> Optional[str]:
    try:
        f = float(freq)
    except (TypeError, ValueError):
        return None
    if f <= 0:
        return None
    return f"{f / 1e6:.4f} MHz" if f > 1e5 else f"{f:.4f} MHz"


def _ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    return f"{seconds // 3600} h ago"


def _channel_label(ch: Dict[str, Any]) -> str:
    freq = _mhz(ch.get("freq"))
    name = ch.get("name") or ""
    if name and freq and freq not in name:
        return f"{name} ({freq})"
    return name or freq or "a channel"


def _conventional_decode_check(chans: List[Dict[str, Any]]) -> Dict[str, str]:
    """P25 conventional (node-26#24): no control channel to lock on to, so the
    proof of decoding is P25 frames (a NAC) heard on the channels — and the one
    failure the owner can't fix is encryption."""
    if not chans:
        return _check("decode", "Decoding", WARN, "Waiting for OP25 to report its channels…")
    encrypted = [c for c in chans if c.get("encrypted")]
    if encrypted:
        from app.internal.conventional import algid_name
        parts = []
        for c in encrypted:
            key = f", key 0x{c['keyid']:04X}" if c.get("keyid") is not None else ""
            parts.append(f"{_channel_label(c)}: {algid_name(c.get('algid'))}{key}")
        # "Everything is encrypted" only when no clear call was ever heard —
        # `encrypted` alone means a channel's most recent call was.
        every = len(encrypted) == len(chans) and not any(c.get("clear_calls") for c in chans)
        return _check(
            "decode", "Decoding", ERROR if every else WARN,
            ("Encrypted — " if every else "Partly encrypted — ") + "; ".join(parts)
            + ". Encrypted calls can't be decoded, so nothing from "
            + ("these channels" if every else "those channels") + " is recorded.",
            "Nothing to fix on the node: the agency encrypts this traffic. "
            + ("Point the node at a different system." if every else "Clear calls are still recorded."),
        )
    heard = [c for c in chans if c.get("nac")]
    listening = ", ".join(_channel_label(c) for c in chans)
    if not heard:
        return _check(
            "decode", "Decoding", OK,
            f"Listening on {listening} (P25 conventional) — no P25 traffic heard yet.",
            "Normal on a quiet channel: conventional channels are silent until someone talks. If it stays "
            "silent through a busy period, check the frequency, antenna and the SDR's PPM.",
        )
    nacs = ", ".join(sorted({f"0x{c['nac']:03X}" for c in heard}))
    return _check("decode", "Decoding", OK, f"Listening on {listening} (P25 conventional) — decoding, NAC {nacs}.")


def active_config(node_cfg) -> Any:
    if node_cfg.override_system_id and node_cfg.override_config:
        return node_cfg.override_config
    return node_cfg.system_config


def build_checks(f: Dict[str, Any]) -> List[Dict[str, str]]:
    """Facts -> ordered checklist. See gather_facts() for the keys."""
    now = f.get("now", time.time())
    checks: List[Dict[str, str]] = []

    # 1. Dashboard password
    if f["default_password"]:
        checks.append(_check(
            "password", "Dashboard password", WARN,
            "Still the factory default — anyone on this network can log in.",
            "Set DASHBOARD_PASSWORD in the node's .env file, then restart the edge-node container.",
        ))
    else:
        checks.append(_check("password", "Dashboard password", OK, "Changed from the default."))

    # 2. C2 enrollment / approval
    c2_url, has_key = f["c2_url"], f["has_api_key"]
    if not c2_url:
        checks.append(_check(
            "c2", "Server (C2) link", OFF,
            "Standalone — no C2 server is set. Local listening below still works.",
            "To join a DRB server, re-run install.sh with an enrollment token.",
        ))
    elif not has_key:
        checks.append(_check(
            "c2", "Server (C2) link", WARN,
            f"Waiting for approval — this node has no API key from {c2_url} yet, so nothing is uploaded.",
            f"Ask a DRB admin to approve node \"{f['node_id']}\" on the Nodes page, then re-run "
            "install.sh (or restart the edge-node container). Local listening below works meanwhile.",
        ))
    else:
        checks.append(_check("c2", "Server (C2) link", OK, f"Approved — API key on file for {c2_url}."))

    # 3. MQTT
    broker = f"{f['mqtt_broker']}:{f['mqtt_port']}"
    if f["mqtt_connected"]:
        checks.append(_check("mqtt", "Live link (MQTT)", OK, f"Connected to {broker}."))
    elif not c2_url and not has_key:
        checks.append(_check(
            "mqtt", "Live link (MQTT)", OFF, f"Not connected to {broker} (expected for a standalone node).",
        ))
    elif not has_key:
        checks.append(_check(
            "mqtt", "Live link (MQTT)", WARN,
            f"Not connected to {broker} — expected until the node is approved (the broker only admits approved nodes).",
            "Fixes itself once the node is approved and has its API key (see above).",
        ))
    elif (f.get("mqtt_last_error") or {}).get("kind") == "refused":
        err = f["mqtt_last_error"]
        checks.append(_check(
            "mqtt", "Live link (MQTT)", ERROR,
            f"{broker} refused this node's login ({err.get('detail')}).",
            "The API key on file is not the one the broker expects — every re-approval issues a new one. "
            "Re-run install.sh to fetch the current key, or ask a DRB admin to re-approve the node.",
        ))
    elif (f.get("mqtt_last_error") or {}).get("kind") == "unreachable":
        err = f["mqtt_last_error"]
        checks.append(_check(
            "mqtt", "Live link (MQTT)", ERROR,
            f"Can't reach {broker} ({err.get('detail')}).",
            f"Check this site's internet connection, DNS, and that outbound port {f['mqtt_port']} isn't "
            "blocked by a firewall. The key is not the problem.",
        ))
    else:
        checks.append(_check(
            "mqtt", "Live link (MQTT)", ERROR,
            f"Not connected to {broker} even though an API key is on file.",
            "If the key was just installed or the node was re-approved, restart the edge-node container "
            "so it loads the key. If it still fails, the key may be stale (every re-approval issues a new "
            "one) — re-run install.sh, and check the node can reach the broker.",
        ))

    # 4. SDR
    sdr_count = f.get("sdr_count")
    if sdr_count is None:
        checks.append(_check(
            "sdr", "SDR receiver", WARN, "Couldn't check — the SDR service isn't answering.",
            "Check the secondary-sdr container is running.",
        ))
    elif sdr_count == 0:
        checks.append(_check(
            "sdr", "SDR receiver", ERROR, "No RTL-SDR dongle detected.",
            "Plug the SDR into a USB port (avoid unpowered hubs), then restart the stack.",
        ))
    else:
        checks.append(_check(
            "sdr", "SDR receiver", OK, f"{sdr_count} SDR{'s' if sdr_count != 1 else ''} detected.",
        ))

    # 5. What to listen to
    src = f.get("source")
    if not src:
        checks.append(_check(
            "source", "What to listen to", WARN, "Nothing chosen yet.",
            "Pick a system or enter a frequency under \"Listen locally\" below.",
        ))
    else:
        origin = "chosen on this dashboard" if src["local"] else "assigned by the server"
        kind = "P25 conventional" if src.get("mode") == "conventional" else src["type"]
        checks.append(_check("source", "What to listen to", OK, f"{src['name']} ({kind}) — {origin}."))

    if f.get("backend") == "trunk-recorder":
        checks.extend(_trunk_recorder_checks(f, src, now))
        return checks

    # 6. OP25
    op25 = f.get("op25_status")
    running = op25 == "running"
    if op25 is None:
        checks.append(_check(
            "op25", "Radio decoder (OP25)", ERROR, "The op25 container isn't answering.",
            "Check the op25 container is running; its log usually says why it stopped.",
        ))
    elif running:
        checks.append(_check("op25", "Radio decoder (OP25)", OK, "Running."))
    elif op25 == "crashed":
        checks.append(_check(
            "op25", "Radio decoder (OP25)", ERROR, "Stopped unexpectedly.",
            "Open \"OP25 log\" below for the reason — usually the SDR is unplugged or in use. Then press Start.",
        ))
    elif not src:
        checks.append(_check("op25", "Radio decoder (OP25)", OFF, "Stopped — choose what to listen to first."))
    else:
        checks.append(_check("op25", "Radio decoder (OP25)", WARN, "Stopped.", "Press Start."))

    # 7. Decoding
    console = f.get("console") or {}
    last_poll = console.get("last_poll_epoch")
    is_analog = bool(src) and str(src.get("type", "")).upper() in ("NBFM", "ANALOG")
    is_conventional = bool(src) and src.get("mode") == "conventional"
    if not running:
        checks.append(_check("decode", "Decoding", OFF, "Starts when OP25 runs."))
    elif last_poll is None or now - last_poll > CONSOLE_STALE_SECONDS:
        checks.append(_check(
            "decode", "Decoding", WARN, "OP25 is running but hasn't reported in yet.",
            "Give it 20 seconds after a start. If this stays, open the OP25 log below.",
        ))
    elif is_analog:
        chans = console.get("channels") or []
        freq = _mhz(chans[0].get("freq")) if chans else None
        checks.append(_check(
            "decode", "Decoding", OK, f"Tuned to {freq or 'the analog frequency'} (analog FM — no decoding needed).",
        ))
    elif is_conventional:
        checks.append(_conventional_decode_check(f.get("conventional") or []))
    else:
        trunk = console.get("trunk") or []
        tsbk = [float(t.get("last_tsbk") or 0) for t in trunk]
        if not trunk:
            checks.append(_check("decode", "Decoding", WARN, "Waiting for OP25 to report control-channel status…"))
        elif max(tsbk) > 0 and now - max(tsbk) < TSBK_STALE_SECONDS:
            sys_ = trunk[tsbk.index(max(tsbk))]
            cc = _mhz(sys_.get("rxchan"))
            checks.append(_check(
                "decode", "Decoding", OK, f"Locked on the control channel{' ' + cc if cc else ''}.",
            ))
        else:
            checks.append(_check(
                "decode", "Decoding", ERROR, "Not decoding the control channel.",
                "Usually a wrong control-channel frequency, a weak signal/antenna, or SDR frequency error (PPM). "
                "Try another control channel for the same system.",
            ))

    # 8. Audio (PulseAudio — recording + Discord path)
    if not f["audio_capturing"]:
        checks.append(_check(
            "audio", "Audio", ERROR if running else OFF,
            "No audio reaching the recorder." if running else "Nothing to hear until OP25 runs.",
            "The op25 container owns the audio path (PulseAudio); restart it if this persists." if running else "",
        ))
    else:
        last_voice = f.get("last_voice_epoch")
        if last_voice is None:
            checks.append(_check("audio", "Audio", OK, "Audio path up — no voice heard yet."))
        elif now - last_voice < 3:
            checks.append(_check("audio", "Audio", OK, "Voice right now."))
        else:
            checks.append(_check("audio", "Audio", OK, f"Audio path up — last voice {_ago(now - last_voice)}."))

    # 9. Local stream (Icecast)
    ice = f.get("icecast_live")
    if ice is None:
        checks.append(_check(
            "stream", "Local stream", ERROR, f"Icecast isn't answering on port {f['icecast_port']}.",
            "Check the icecast container is running.",
        ))
    elif ice:
        checks.append(_check("stream", "Local stream", OK, "Live — press play in the player below."))
    elif running:
        checks.append(_check(
            "stream", "Local stream", WARN, "OP25 is running but nothing is feeding the stream yet.",
            "It normally appears within ~20 seconds of a start; if not, check the OP25 log below.",
        ))
    else:
        checks.append(_check("stream", "Local stream", OFF, "Goes live when OP25 runs."))

    return checks


def _trunk_recorder_checks(f: Dict[str, Any], src: Optional[Dict[str, Any]], now: float) -> List[Dict[str, str]]:
    """Checks 6-9 when Trunk Recorder is the recorder (node-26#45)."""
    tr = f.get("tr") or {}
    checks: List[Dict[str, str]] = []
    status = tr.get("status")
    state = (status or {}).get("state")
    running = False
    label = "Radio recorder (Trunk Recorder)"
    if tr.get("config_error"):
        checks.append(_check(
            "recorder", label, ERROR, f"Couldn't build its config — {tr['config_error']}",
            "Fix the system's channel list (or TR_* settings in .env); the node retries on the next config push.",
        ))
    elif status is None:
        checks.append(_check(
            "recorder", label, ERROR, "The trunk-recorder service isn't running.",
            "Set COMPOSE_PROFILES=trunk-recorder in the node's .env, then run `docker compose up -d`.",
        ))
    elif status.get("stale"):
        checks.append(_check(
            "recorder", label, ERROR, f"Its supervisor stopped reporting {_ago(status.get('age_seconds') or 0)}.",
            "Check the trunk-recorder container: `docker compose logs trunk-recorder`.",
        ))
    elif state == "running":
        running = True
        up = now - float(status.get("started_at") or now)
        restarts = int(status.get("restarts") or 0)
        checks.append(_check(
            "recorder", label, OK if restarts < 3 else WARN,
            f"Running for {_ago(up).replace(' ago', '')}"
            + (f" — restarted {restarts} times since the container started." if restarts else "."),
            "" if restarts < 3 else "Frequent restarts: `docker compose logs trunk-recorder` says why.",
        ))
    elif state == "crashed":
        checks.append(_check(
            "recorder", label, ERROR, f"Exited (code {status.get('last_exit')}) — restarting.",
            "Usually the SDR is unplugged or held by another program. `docker compose logs trunk-recorder`.",
        ))
    elif state == "stopped":
        checks.append(_check("recorder", label, WARN, "Stopped from the dashboard.", "Press Start."))
    elif not src:
        checks.append(_check("recorder", label, OFF, "Waiting — choose what to listen to first."))
    else:
        checks.append(_check("recorder", label, WARN, "Waiting for its config…"))

    mode = (src or {}).get("mode")
    if not running:
        checks.append(_check("decode", "Decoding", OFF, "Starts when Trunk Recorder runs."))
    elif mode == "trunked":
        rate = tr.get("decode_rate")
        age = tr.get("log_age_seconds")
        if rate is None or age is None or age > 30:
            checks.append(_check(
                "decode", "Decoding", WARN, "No control-channel report from Trunk Recorder yet.",
                "Give it 30 seconds after a start; if it stays, `docker compose logs trunk-recorder`.",
            ))
        elif rate > 0:
            checks.append(_check("decode", "Decoding", OK, f"Decoding the control channel ({rate:g} messages/sec)."))
        else:
            checks.append(_check(
                "decode", "Decoding", ERROR, "Not decoding the control channel (0 messages/sec).",
                "Usually a wrong control-channel frequency, a weak signal/antenna, or SDR frequency error (PPM).",
            ))
    else:
        checks.append(_check(
            "decode", "Decoding", OK, "Listening (conventional) — channels record when their squelch opens.",
            "If calls never arrive through a busy period, the squelch (TR_SQUELCH_DB or the system's "
            "squelch_threshold) may be set too high.",
        ))

    last = tr.get("last_call_epoch")
    backlog = int(tr.get("inbox_backlog") or 0)
    if not running and not last:
        checks.append(_check("audio", "Recordings", OFF, "Nothing recorded yet."))
    elif backlog > 20:
        checks.append(_check(
            "audio", "Recordings", WARN, f"{backlog} finished calls waiting to be sent.",
            "Normal for a minute after the server link comes back; if it keeps growing, check the C2 link.",
        ))
    elif last:
        extra = f", {tr['encrypted_skipped']} encrypted skipped" if tr.get("encrypted_skipped") else ""
        checks.append(_check(
            "audio", "Recordings", OK,
            f"Last call {_ago(now - last)} ({tr.get('calls_ingested', 0)} since start{extra}).",
        ))
    else:
        checks.append(_check("audio", "Recordings", OK, "Recorder up — no call yet."))

    live = tr.get("live") or {}
    heard = live.get("last_packet_epoch")
    checks.append(_check(
        "stream", "Live audio", OK if heard else OFF,
        ("Feeding Discord — last audio " + _ago(now - heard) + "." if heard else "Feeds Discord once a call is heard.")
        + " The local browser stream isn't available with Trunk Recorder.",
    ))
    return checks


async def icecast_live() -> Optional[bool]:
    """True if the configured mount has a source, False if not, None if Icecast is down."""
    url = f"http://{settings.icecast_host}:{settings.icecast_port}/status-json.xsl"
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            r = await client.get(url)
            r.raise_for_status()
            sources = (r.json().get("icestats") or {}).get("source") or []
    except Exception as e:
        logger.debug(f"Icecast status check failed: {e}")
        return None
    if isinstance(sources, dict):
        sources = [sources]
    return any(str(s.get("listenurl", "")).endswith(settings.icecast_mount) for s in sources)


async def gather_facts() -> Dict[str, Any]:
    from app.internal import auth, credentials
    from app.internal.call_recorder import call_recorder
    from app.internal.config_manager import load_node_config
    from app.internal.metadata_watcher import metadata_watcher
    from app.internal.mqtt_manager import mqtt_manager
    from app.internal.op25_client import op25_client
    from app.internal.radio_config import system_mode
    from app.internal.secondary_sdr_client import secondary_sdr_client

    node_cfg = load_node_config()
    cfg = active_config(node_cfg)
    op25 = await op25_client.status()

    devs = await secondary_sdr_client.devices()
    sdr_count: Optional[int] = len(devs) if devs is not None else None
    if not sdr_count:
        # op25's own lsusb count — its :stable image may lack lsusb and say 0,
        # so only trust a positive answer from it.
        op25_devs = await op25_client.devices()
        if op25_devs and op25_devs.get("count"):
            sdr_count = op25_devs["count"]

    activity = call_recorder.audio_activity()
    from app.internal import trunk_recorder as tr
    return {
        "now": time.time(),
        "backend": settings.recorder_backend,
        "tr": tr.trunk_recorder.health_facts() if tr.enabled() else None,
        "node_id": settings.node_id,
        "default_password": auth.is_using_default_password(),
        "c2_url": settings.c2_url,
        "has_api_key": bool(credentials.get_api_key()),
        "mqtt_connected": mqtt_manager.is_connected,
        "mqtt_last_error": mqtt_manager.last_error,
        "mqtt_broker": settings.mqtt_broker,
        "mqtt_port": settings.mqtt_port,
        "sdr_count": sdr_count,
        "source": None if cfg is None else {
            "name": cfg.name, "type": cfg.type, "system_id": cfg.system_id,
            "mode": system_mode(cfg.type, cfg.config),
            "local": bool(node_cfg.override_system_id and node_cfg.override_config),
        },
        "conventional": metadata_watcher.conventional_channels,
        "op25_status": (op25 or {}).get("status") if op25 is not None else None,
        "console": metadata_watcher.console_state,
        "audio_capturing": call_recorder.is_capturing,
        "last_voice_epoch": activity.last_voice_epoch,
        "icecast_live": await icecast_live(),
        "icecast_port": settings.icecast_port,
    }
