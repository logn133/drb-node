import asyncio
import json
from datetime import datetime, timezone
from collections import deque
from typing import Optional, Callable, Awaitable, Dict, Any
import paho.mqtt.client as mqtt
from app.config import settings
from app.internal.logger import logger
from app.internal import credentials, key_pickup

CommandCallback = Callable[[Dict[str, Any]], Awaitable[None]]
ConfigCallback = Callable[[Dict[str, Any]], Awaitable[None]]
ApiKeyCallback = Callable[[Dict[str, Any]], Awaitable[None]]

# CONNACK codes meaning "your credentials are wrong", as paho reports them:
# MQTT 3.1.1 return codes 4/5, and the MQTT 5 reason codes paho maps them to
# (134 Bad user name or password, 135 Not authorized).
_AUTH_FAILURE_CODES = {4, 5, 134, 135}

# Credential-recovery backoff (node-26#14). The pickup endpoint is cheap, but a
# node that is simply not approved yet must not hammer C2 for days.
RECOVERY_MIN_DELAY = 30
RECOVERY_MAX_DELAY = 900


def _is_auth_failure(reason_code) -> bool:
    value = getattr(reason_code, "value", reason_code)
    if isinstance(value, int) and value in _AUTH_FAILURE_CODES:
        return True
    text = str(reason_code).lower()
    return "not authori" in text or "bad user name or password" in text


class MQTTManager:
    def __init__(self):
        self._client: Optional[mqtt.Client] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._connected = False
        self._connect_task: Optional[asyncio.Task] = None
        # The api_key the CURRENT paho client was built with — not necessarily
        # credentials.get_api_key(), which can move ahead of it (MQTT key push,
        # install.sh writing the file). Recovery compares against this.
        self._client_key: Optional[str] = None
        self._recovery_task: Optional[asyncio.Task] = None

        self.on_command: Optional[CommandCallback] = None
        self.on_config_push: Optional[ConfigCallback] = None
        self.on_api_key: Optional[ApiKeyCallback] = None

        self._offline_buffer = deque(maxlen=settings.offline_call_buffer_size)

        nid = settings.node_id
        self._t_checkin = f"nodes/{nid}/checkin"
        self._t_status = f"nodes/{nid}/status"
        self._t_metadata = f"nodes/{nid}/metadata"
        self._t_commands = f"nodes/{nid}/commands"
        self._t_config = f"nodes/{nid}/config"
        # TODO(mqtt-cutover): dead once enrollment lands client-side. This
        # was the pre-dynsec key-delivery path (server retain-publishes the
        # api_key here after admin approval; node asks for redelivery via
        # _t_key_request if none shows up). Under dynsec a node with no
        # api_key can't authenticate to the broker at all — see
        # _build_client() — so this subscribe is only ever reachable while
        # still using the legacy mqtt_user/mqtt_pass fallback against a
        # pre-cutover broker. Left in as the rollback path per
        # MQTT-PUBLIC-AUTH-PLAN.md; remove together with the server's
        # matching TODO(mqtt-cutover) markers once enrollment replaces it.
        self._t_api_key = f"nodes/{nid}/api_key"
        self._t_key_request = f"nodes/{nid}/key_request"
        self._t_discovery = "nodes/discovery/request"

    def _build_client(self) -> mqtt.Client:
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=settings.node_id,
        )

        api_key = credentials.get_api_key()
        self._client_key = api_key
        if api_key:
            # Post-cutover auth: broker's dynsec plugin authenticates this
            # exact (username, password) pair as this node's own client — see
            # Server/drb-c2-core/app/internal/dynsec.py upsert_node_client()
            # and MQTT-PUBLIC-AUTH-PLAN.md. node_id doubles as the dynsec
            # username AND the %u substitution in the "node" role's
            # nodes/%u/# ACL pattern, so it must match exactly what C2 has on
            # file for this node (it always does — node_id is not operator
            # editable post-provisioning).
            client.username_pw_set(settings.node_id, api_key)
        elif settings.mqtt_user:
            # Legacy fallback — only valid against a pre-cutover broker still
            # using mosquitto's old password_file auth. See config.py's
            # mqtt_user/mqtt_pass docstring. Not accepted by a dynsec broker.
            client.username_pw_set(settings.mqtt_user, settings.mqtt_pass)
        else:
            # No api_key on disk and no legacy shared login configured. A
            # dynsec broker (allow_anonymous false) refuses this outright.
            # connect() starts _recover_credentials() in this case, which
            # fetches the key over HTTPS (key_pickup.py) and rebuilds this
            # client with it — node-26#14. paho's reconnect_delay_set() below
            # bounds the refusal loop meanwhile (2..60s backoff).
            logger.warning(
                "No API key on disk and no legacy MQTT_USER configured — "
                "connecting without credentials while the key is fetched from C2."
            )

        if settings.mqtt_tls:
            # No arguments = system CA store + ssl.CERT_REQUIRED (verified
            # against paho's tls_set() source/docstring — unverified by
            # running anything, per instruction). The broker presents a real
            # Let's Encrypt cert for mqtt.<domain>:8883, so default
            # verification is exactly correct: do not pass ca_certs, do not
            # call tls_insecure_set(True).
            client.tls_set()

        lwt = json.dumps({
            "node_id": settings.node_id,
            "status": "offline",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        client.will_set(self._t_status, lwt, qos=1, retain=True)

        client.reconnect_delay_set(min_delay=2, max_delay=60)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        return client

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            self._connected = True
            client.subscribe(self._t_commands, qos=1)
            client.subscribe(self._t_config, qos=1)
            client.subscribe(self._t_api_key, qos=2)  # TODO(mqtt-cutover): see _t_api_key comment above
            client.subscribe(self._t_discovery, qos=0)
            logger.info("MQTT connected.")
            asyncio.run_coroutine_threadsafe(self._publish_checkin(), self._loop)
            # TODO(mqtt-cutover): see _t_api_key comment above
            asyncio.run_coroutine_threadsafe(self._maybe_request_key(), self._loop)
            asyncio.run_coroutine_threadsafe(self._flush_offline_buffer(), self._loop)
        else:
            logger.error(f"MQTT connect refused: {reason_code}")
            if _is_auth_failure(reason_code) and self._loop is not None:
                # paho thread -> event loop. A refused login never fixes itself
                # by retrying the same password, so go get the right one.
                self._loop.call_soon_threadsafe(self._start_recovery, f"broker refused login ({reason_code})")

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        self._connected = False
        logger.warning(f"MQTT disconnected: {reason_code}")

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
        except Exception:
            payload = msg.payload.decode()

        if msg.topic == self._t_commands and self.on_command:
            asyncio.run_coroutine_threadsafe(self.on_command(payload), self._loop)
        elif msg.topic == self._t_config and self.on_config_push:
            asyncio.run_coroutine_threadsafe(self.on_config_push(payload), self._loop)
        elif msg.topic == self._t_api_key and self.on_api_key:
            asyncio.run_coroutine_threadsafe(self.on_api_key(payload), self._loop)
        elif msg.topic == self._t_discovery:
            asyncio.run_coroutine_threadsafe(self._publish_checkin(), self._loop)

    async def connect(self):
        self._loop = asyncio.get_event_loop()
        self._client = self._build_client()
        self._connect_task = asyncio.create_task(self._connect_with_retry())
        if not self._client_key and not settings.mqtt_user:
            self._start_recovery("no API key at startup")

    # ------------------------------------------------------------------
    # Credential self-heal (node-26#14)
    #
    # A node with no key, or a stale one, used to sit in an MQTT refusal loop
    # forever: key delivery was MQTT-only, and credentials were read once at
    # startup. Now: re-read credentials.json (install.sh may have written it),
    # else fetch from C2 over HTTPS with the pickup secret; if that yields a
    # key different from the one the client is using, rebuild the client with
    # it in place. No container restart.
    # ------------------------------------------------------------------
    def _start_recovery(self, why: str) -> None:
        if self._recovery_task and not self._recovery_task.done():
            return
        logger.warning(f"MQTT credential recovery started: {why}")
        self._recovery_task = asyncio.create_task(self._recover_credentials())

    async def _recover_credentials(self) -> None:
        delay = RECOVERY_MIN_DELAY
        while True:
            tried = self._client_key
            key = credentials.reload_api_key()
            if not key or key == tried:
                fetched = await key_pickup.fetch_api_key()
                if fetched and fetched != tried:
                    credentials.save_api_key(fetched)
                    key = fetched
            if key and key != tried:
                logger.info("MQTT credential recovery: new API key in hand — reconnecting with it.")
                await self._rebuild_client()
                return
            if self._connected:
                return
            if key and key == tried:
                logger.error(
                    "MQTT credential recovery: C2 returns the same key the broker refuses. The broker's login for "
                    "this node is out of sync with C2 (dynsec) — an admin re-approve re-syncs it without rotating."
                )
            logger.info(f"MQTT credential recovery: retrying in {delay}s.")
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECOVERY_MAX_DELAY)

    async def _rebuild_client(self) -> None:
        """Swap the paho client for one built from the current credentials."""
        old = self._client
        if self._connect_task and not self._connect_task.done():
            self._connect_task.cancel()
        if old is not None:
            # loop_stop() joins paho's thread, which can be mid-backoff — off the event loop.
            await asyncio.to_thread(old.loop_stop)
            try:
                old.disconnect()
            except Exception:
                pass
        self._connected = False
        self._client = self._build_client()
        self._connect_task = asyncio.create_task(self._connect_with_retry())

    def update_api_key(self, key: str) -> None:
        """A key arrived while connected (legacy MQTT push / reissue): use it on
        the next reconnect without dropping the current, working session."""
        if self._client is not None and key != self._client_key:
            self._client.username_pw_set(settings.node_id, key)
            self._client_key = key

    async def _connect_with_retry(self):
        """Attempt the initial TCP connect, retrying with backoff until it succeeds."""
        delay = 5
        logger.info(f"MQTT connecting to {settings.mqtt_broker}:{settings.mqtt_port}")
        while True:
            try:
                self._client.connect(settings.mqtt_broker, settings.mqtt_port, keepalive=60)
                self._client.loop_start()
                # paho loop_start + reconnect_delay_set handles all subsequent reconnects
                return
            except Exception as e:
                logger.warning(f"MQTT connect failed ({e}) — retrying in {delay}s")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    async def disconnect(self):
        if self._recovery_task:
            self._recovery_task.cancel()
        if self._connect_task:
            self._connect_task.cancel()
        if self._client:
            await self.publish_status("offline")
            self._client.loop_stop()
            self._client.disconnect()

    async def publish_status(self, status: str, extra: dict = None):
        payload = {
            "node_id": settings.node_id,
            "status": status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **(extra or {}),
        }
        self._publish(self._t_status, payload, qos=1, retain=True)

    async def publish_metadata(self, event_type: str, data: dict):
        payload = {
            "event": event_type,
            "node_id": settings.node_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **data,
        }
        if not self._connected:
            if event_type == "call_end":
                self._offline_buffer.append((self._t_metadata, payload))
                logger.warning(f"MQTT offline. Buffered call_end event for {data.get('call_id')}")
            else:
                logger.debug(f"MQTT offline. Dropping metadata event: {event_type}")
        else:
            self._publish(self._t_metadata, payload, qos=1)

    async def _flush_offline_buffer(self):
        if not self._offline_buffer:
            return
        count = len(self._offline_buffer)
        logger.info(f"Relaying {count} buffered call_end events from offline queue.")
        while self._offline_buffer:
            topic, payload = self._offline_buffer.popleft()
            self._publish(topic, payload, qos=1)

    async def _maybe_request_key(self):
        """After connecting, wait for any retained api_key message to arrive.
        If no key materialises within 5 seconds, ask the server to re-deliver it."""
        await asyncio.sleep(5)
        if not credentials.get_api_key():
            logger.info("No API key on disk — requesting re-delivery from C2 server.")
            self._publish(self._t_key_request, {}, qos=1)

    async def publish_checkin(self):
        await self._publish_checkin()

    async def _publish_checkin(self):
        from app.internal.discord_radio import radio_bot
        from app.internal.config_manager import load_node_config
        from app.internal.op25_client import op25_client
        from app.internal.secondary_sdr_client import secondary_sdr_client
        config = load_node_config()
        devices = await op25_client.devices()
        payload = {
            "node_id": settings.node_id,
            "name": settings.node_name,
            "lat": settings.node_lat,
            "lon": settings.node_lon,
            "discord_connected": radio_bot.is_connected,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "node_type": config.node_type,
            "is_overridden": config.override_system_id is not None and config.node_type != "portable",
            "override_system_id": config.override_system_id,
            "enforce_override_timeout": config.enforce_override_timeout,
            "secondary_sdr_mode": config.secondary_sdr_mode,
            "secondary_sdr_priority": config.secondary_sdr_priority,
        }
        payload["sdr_pins"] = config.sdr_pins
        secondary = await secondary_sdr_client.status()
        # Unreachable secondary-sdr: say so with explicit nulls. C2 only
        # overwrites keys present in the checkin, so omitting them would leave
        # a dead decoder showing "Running" forever.
        payload["secondary_sdr_running"] = None
        payload["sdr_devices"] = None
        payload["op25_sdr_serial"] = None
        if secondary is not None:
            payload["secondary_sdr_running"] = [r["mode"] for r in secondary.get("running", [])]
            if secondary.get("devices") is not None:
                payload["sdr_devices"] = [
                    {k: d.get(k) for k in ("index", "serial", "name", "duplicate_serial")}
                    for d in secondary["devices"]
                ]
                from app.internal.sdr_settings import op25_serial
                payload["op25_sdr_serial"] = op25_serial(config, secondary["devices"])
        # Best-effort hardware report — omit rather than guess. Prefer the
        # secondary-sdr container's count: op25's :stable image has no lsusb and
        # its /op25/devices answers 0 rather than "unknown" when that fails. A
        # node running op25 has at least one SDR, so 0 is never a real reading.
        count = secondary.get("sdr_count") if secondary is not None else None
        if not count and devices:
            count = devices.get("count") or None
        if count is not None:
            payload["sdr_count"] = count
        self._publish(self._t_checkin, payload, qos=1)

    def _publish(self, topic: str, payload: dict, qos: int = 0, retain: bool = False):
        if self._client and self._connected:
            self._client.publish(topic, json.dumps(payload), qos=qos, retain=retain)
        else:
            logger.debug(f"MQTT not connected, dropping publish to {topic}")

    async def heartbeat_loop(self):
        while True:
            if self._connected:
                await self._publish_checkin()
            await asyncio.sleep(30)

    @property
    def is_connected(self) -> bool:
        return self._connected


mqtt_manager = MQTTManager()
