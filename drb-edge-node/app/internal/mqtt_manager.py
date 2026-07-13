import asyncio
import json
from datetime import datetime, timezone
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

        nid = settings.node_id
        self._t_checkin = f"nodes/{nid}/checkin"
        self._t_status = f"nodes/{nid}/status"
        self._t_metadata = f"nodes/{nid}/metadata"
        self._t_commands = f"nodes/{nid}/commands"
        self._t_config = f"nodes/{nid}/config"
        self._t_api_key = f"nodes/{nid}/api_key"
        self._t_key_request = f"nodes/{nid}/key_request"
        self._t_discovery = "nodes/discovery/request"

    def _build_client(self) -> mqtt.Client:
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=settings.node_id,
        )
        if settings.mqtt_user:
            client.username_pw_set(settings.mqtt_user, settings.mqtt_pass)

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
            client.subscribe(self._t_api_key, qos=2)
            client.subscribe(self._t_discovery, qos=0)
            logger.info("MQTT connected.")
            asyncio.run_coroutine_threadsafe(self._publish_checkin(), self._loop)
            asyncio.run_coroutine_threadsafe(self._maybe_request_key(), self._loop)
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
        self._publish(self._t_metadata, payload, qos=1)

    async def _maybe_request_key(self):
        """After connecting, wait for any retained api_key message to arrive.
        If no key materialises within 5 seconds, ask the server to re-deliver it."""
        await asyncio.sleep(5)
        if not credentials.get_api_key():
            logger.info("No API key on disk — requesting re-delivery from C2 server.")
            self._publish(self._t_key_request, {}, qos=1)

    async def _publish_checkin(self):
        from app.internal.discord_radio import radio_bot
        from app.internal.config_manager import load_node_config
        config = load_node_config()
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
        }
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
