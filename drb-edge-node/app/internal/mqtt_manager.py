import asyncio
import json
from datetime import datetime, timezone
from collections import deque
from typing import Optional, Callable, Awaitable, Dict, Any
import paho.mqtt.client as mqtt
from app.config import settings
from app.internal.logger import logger
from app.internal import credentials

CommandCallback = Callable[[Dict[str, Any]], Awaitable[None]]
ConfigCallback = Callable[[Dict[str, Any]], Awaitable[None]]
ApiKeyCallback = Callable[[Dict[str, Any]], Awaitable[None]]


class MQTTManager:
    def __init__(self):
        self._client: Optional[mqtt.Client] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._connected = False
        self._connect_task: Optional[asyncio.Task] = None

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
            # dynsec broker (allow_anonymous false) refuses this outright —
            # expected, not a bug to route around here: this node hasn't been
            # enrolled/approved yet, and the enrollment flow that would fix
            # that client-side is a later, separate pass (out of scope here;
            # see MQTT-PUBLIC-AUTH-PLAN.md). paho's reconnect_delay_set()
            # below bounds the retry rate (2..60s exponential backoff), so
            # this degrades to a slow, clearly-logged refusal loop via
            # _on_connect's "MQTT connect refused" line — not a hot spin.
            logger.warning(
                "No API key on disk and no legacy MQTT_USER configured — "
                "connecting without credentials; the broker is expected to "
                "refuse this until the node is enrolled/approved."
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
        secondary = await secondary_sdr_client.status()
        if secondary is not None:
            payload["secondary_sdr_running"] = [r["mode"] for r in secondary.get("running", [])]
        # Best-effort hardware report — omit rather than guess. op25's :stable
        # image has no lsusb, so fall back to the secondary-sdr container's count.
        count = devices.get("count") if devices else None
        if count is None and secondary is not None:
            count = secondary.get("sdr_count")
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
