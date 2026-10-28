"""
Discord radio streams on the edge, driven by desired state (node-26#25,
server-26#203/#204).

C2 retains ``nodes/{id}/discord/desired`` = the FULL list of streams this node
should carry: ``{seq, streams: [{stream_id, guild_id, channel_id, token_id,
system_name, ...}]}``. This module reconciles toward that list and publishes
``nodes/{id}/discord/actual`` = ``{applied_seq, sessions: [token_id],
streams: {stream_id: {state, error, code}}}`` after every change.

Rules this enforces by construction:

* One gateway session per leased token; a token no stream needs any more is
  logged OUT (``client.close()``), not just disconnected from voice. C2 frees
  the token only after an ``actual`` with ``applied_seq`` >= the drop and the
  token absent from ``sessions`` — so one token is never logged in on two
  nodes (node-26#25 item 1).
* One voice connection per (token, guild); one token can serve many guilds.
* The watchdog is a separate reconcile loop that never closes itself: closing
  a session cannot cancel it (node-26#25 item 2).
* No message-content intent, no ``@bot leave/join`` mention controls — voice
  is controlled only through C2 (node-26#25 item 3).
* MQTT-loss fail-safe: if the broker has been unreachable for
  ``discord_mqtt_loss_stop_seconds`` every radio bot is logged out, because C2
  returns this node's tokens to the pool after its own (longer) grace.

Bot token strings never travel over MQTT: they are fetched over HTTPS from C2
(``GET /discord/node/{node}/tokens/{id}``, node API key), which hands a token
only to the node holding its lease, and are kept in memory only.

Audio: each voice connection plays the PulseAudio monitor (``-f pulse`` on
``settings.pulse_source``) — the accuracy path, never Icecast.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import discord

from app.config import settings
from app.internal import pulse
from app.internal.logger import logger

BOT_READY_TIMEOUT = 15     # seconds to wait for a Discord login to become ready
WATCHDOG_INTERVAL = 15     # seconds between reconcile passes
STREAM_RETRY_DELAY = 5     # seconds to back off before re-arming an audio source
REPORT_EVERY_TICKS = 4     # unconditional actual-state report every ~60s
_VOICE_TYPES = (discord.ChannelType.voice, discord.ChannelType.stage_voice)


@dataclass
class _Session:
    token_id: str
    client: object
    task: Optional[asyncio.Task] = None
    presence: Optional[str] = None

    def alive(self) -> bool:
        return self.task is not None and not self.task.done() and not self.client.is_closed()


@dataclass
class _Voice:
    stream_id: str
    token_id: str
    guild_id: int
    channel_id: int
    vc: Optional[object] = None
    state: str = "connecting"
    error: Optional[str] = None
    code: Optional[str] = None


def _join_refusal(guild, channel) -> Optional[tuple[str, str]]:
    """Why Discord would refuse this bot's voice join, or None.

    Discord does not answer a join the bot lacks permission for — the gateway
    just never sends the voice-server update, discord.py times out after 30s,
    and the admin page used to read "(TimeoutError: )". Checking first turns
    that into an instruction. Best effort: if the bot's own member or the
    permission data is not cached, let the join go ahead.
    """
    me = getattr(guild, "me", None)
    permissions_for = getattr(channel, "permissions_for", None)
    if me is None or permissions_for is None:
        return None
    try:
        perms = permissions_for(me)
    except Exception:
        return None
    missing = [label for attr, label in (("view_channel", "View Channel"), ("connect", "Connect"))
               if not getattr(perms, attr, True)]
    if getattr(channel, "type", None) == discord.ChannelType.voice and not getattr(perms, "speak", True):
        missing.append("Speak")
    if missing:
        return ("missing_permissions",
                f"The radio bot lacks {', '.join(missing)} on that voice channel — "
                "allow it in the channel's permission settings in Discord.")
    limit = getattr(channel, "user_limit", 0) or 0
    # The bot's own lingering voice state (a dropped connection) is not a
    # slot it needs; counting it would block the rejoin of a full channel.
    me_id = getattr(me, "id", None)
    members = [m for m in (getattr(channel, "members", None) or [])
               if me_id is None or getattr(m, "id", None) != me_id]
    if limit and len(members) >= limit and not getattr(perms, "move_members", False):
        return ("channel_full", f"That voice channel is full ({len(members)}/{limit}) — "
                                "raise its user limit or free a slot.")
    return None


def _default_client_factory():
    intents = discord.Intents.none()
    intents.guilds = True
    intents.voice_states = True
    return discord.Client(intents=intents)


def _default_audio_factory():
    # before_options land ahead of -i:  ffmpeg -f pulse -i drb_sink.monitor …
    source = discord.FFmpegPCMAudio(settings.pulse_source, before_options="-f pulse")
    return discord.PCMVolumeTransformer(source, volume=1.0)


async def _default_token_fetcher(token_id: str) -> Optional[str]:
    import httpx
    from app.internal import credentials
    api_key = credentials.get_api_key()
    if not settings.c2_url or not api_key:
        logger.error("Cannot fetch a radio-bot token: C2_URL or the node API key is missing.")
        return None
    url = f"{settings.c2_url.rstrip('/')}/discord/node/{settings.node_id}/tokens/{token_id}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, headers={"Authorization": f"Bearer {api_key}"})
            if r.status_code != 200:
                logger.error(f"C2 refused radio-bot token {token_id}: HTTP {r.status_code}")
                return None
            return r.json().get("token")
    except Exception as e:
        logger.error(f"Fetching radio-bot token {token_id} failed: {e}")
        return None


async def _default_publisher(payload: dict) -> None:
    from app.internal.mqtt_manager import mqtt_manager
    mqtt_manager.publish_discord_actual(payload)


def _default_mqtt_up() -> bool:
    from app.internal.mqtt_manager import mqtt_manager
    return mqtt_manager.is_connected


class RadioManager:
    def __init__(
        self,
        client_factory: Callable[[], object] = _default_client_factory,
        token_fetcher: Callable[[str], Awaitable[Optional[str]]] = _default_token_fetcher,
        publisher: Callable[[dict], Awaitable[None]] = _default_publisher,
        audio_factory: Callable[[], object] = _default_audio_factory,
        mqtt_up: Callable[[], bool] = _default_mqtt_up,
        ready_timeout: float = BOT_READY_TIMEOUT,
    ):
        self._client_factory = client_factory
        self._fetch_token = token_fetcher
        self._publish = publisher
        self._audio_factory = audio_factory
        self._mqtt_up = mqtt_up
        self._ready_timeout = ready_timeout

        self._desired: dict[str, dict] = {}
        self._applied_seq: Optional[int] = None
        self._sessions: dict[str, _Session] = {}
        self._voices: dict[str, _Voice] = {}
        self._session_errors: dict[str, str] = {}
        self._tokens: dict[str, str] = {}          # token_id -> string, memory only
        self._lock = asyncio.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._watchdog: Optional[asyncio.Task] = None
        self._mqtt_down_since: Optional[float] = None
        self._failsafe = False
        self._paused = False
        # The applied seq at the moment of a local pause (node-26#34). The
        # broker redelivers the retained desired message on every reconnect
        # with the SAME seq; only a strictly newer seq from C2 lifts the pause.
        self._paused_at_seq: Optional[int] = None
        self._ticks = 0

    # ------------------------------------------------------------------
    # Inputs
    # ------------------------------------------------------------------

    async def apply_desired(self, payload: dict) -> None:
        """Handle a (retained) desired-state message from C2."""
        if not isinstance(payload, dict):
            return
        seq = payload.get("seq")
        seq = seq if isinstance(seq, int) and not isinstance(seq, bool) else None
        if seq is not None and self._applied_seq is not None and seq < self._applied_seq:
            logger.warning(f"Ignoring stale Discord desired state seq {seq} (applied {self._applied_seq})")
            return
        wanted = {}
        for s in payload.get("streams") or []:
            try:
                wanted[str(s["stream_id"])] = {
                    "stream_id": str(s["stream_id"]), "guild_id": int(s["guild_id"]),
                    "channel_id": int(s["channel_id"]), "token_id": str(s["token_id"]),
                    "system_name": s.get("system_name"),
                }
            except (KeyError, TypeError, ValueError):
                logger.error(f"Malformed Discord desired stream entry ignored: {s!r}")
        self._ensure_watchdog()
        self._desired = wanted
        if self._paused and self._redelivery_of_paused_state(seq):
            logger.info(f"Discord desired seq {seq} is a redelivery — staying paused locally.")
        else:
            self._paused = False
            self._paused_at_seq = None
        await self.reconcile()
        if seq is not None:
            self._applied_seq = seq
        await self.publish_actual()

    def _redelivery_of_paused_state(self, seq: Optional[int]) -> bool:
        """True when `seq` is not newer than what was applied at pause time."""
        return seq is not None and self._paused_at_seq is not None and seq <= self._paused_at_seq

    async def pause_locally(self) -> None:
        """Local-dashboard stop: log every bot out until C2 sends a NEW list
        (a seq strictly greater than the one applied now — node-26#34)."""
        self._paused = True
        self._paused_at_seq = self._applied_seq
        await self.reconcile()
        await self.publish_actual()

    # ------------------------------------------------------------------
    # Reconcile
    # ------------------------------------------------------------------

    def _target(self) -> dict[str, dict]:
        return {} if (self._paused or self._failsafe) else self._desired

    async def reconcile(self) -> None:
        async with self._lock:
            target = self._target()
            # 1) voice connections no longer wanted (or re-pointed)
            for sid, v in list(self._voices.items()):
                d = target.get(sid)
                if not d or (d["token_id"], d["guild_id"], d["channel_id"]) != (v.token_id, v.guild_id, v.channel_id):
                    await self._disconnect(v)
                    del self._voices[sid]
            # 2) gateway sessions no stream needs: LOG OUT, not just leave voice
            needed = {d["token_id"] for d in target.values()}
            for tid in list(self._sessions):
                if tid not in needed:
                    await self._close_session(tid)
            for tid in list(self._tokens):
                if tid not in needed:
                    self._tokens.pop(tid, None)
            # 3) sessions + voice for everything wanted
            for sid, d in sorted(target.items()):
                v = self._voices.get(sid)
                if v is None:
                    v = self._voices[sid] = _Voice(sid, d["token_id"], d["guild_id"], d["channel_id"])
                sess = await self._ensure_session(d["token_id"])
                if sess is None:
                    v.state, v.code = "error", "login_failed"
                    v.error = self._session_errors.get(d["token_id"], "The radio bot could not log in to Discord.")
                    continue
                await self._ensure_voice(sess, v, d)
            # 4) presence follows voice: "Listening to <system>" only while a
            #    voice connection for that bot is actually up.
            for sess in list(self._sessions.values()):
                await self._sync_presence(sess, target)

    async def _ensure_session(self, token_id: str) -> Optional[_Session]:
        sess = self._sessions.get(token_id)
        if sess and sess.alive():
            return sess
        if sess:
            await self._close_session(token_id)
        token = self._tokens.get(token_id) or await self._fetch_token(token_id)
        if not token:
            self._session_errors[token_id] = "DRB would not hand this node the radio bot's token."
            return None
        client = self._client_factory()
        sess = _Session(token_id, client)
        ready = asyncio.Event()

        async def on_ready():
            ready.set()
        client.event(on_ready)
        sess.task = asyncio.create_task(client.start(token))
        waiter = asyncio.create_task(ready.wait())
        done, _ = await asyncio.wait({waiter, sess.task}, timeout=self._ready_timeout,
                                     return_when=asyncio.FIRST_COMPLETED)
        if waiter not in done:
            waiter.cancel()
            err = sess.task.exception() if sess.task.done() and not sess.task.cancelled() else None
            msg = f"{type(err).__name__}: {err}" if err else f"not ready after {self._ready_timeout:.0f}s"
            logger.error(f"Radio bot {token_id} failed to log in: {msg}")
            self._session_errors[token_id] = f"The radio bot could not log in to Discord ({msg[:120]})."
            await self._shutdown_client(sess)
            if isinstance(err, discord.LoginFailure):
                self._tokens.pop(token_id, None)
            return None
        self._tokens[token_id] = token
        self._sessions[token_id] = sess
        self._session_errors.pop(token_id, None)
        logger.info(f"Radio bot {token_id} logged in as {getattr(client, 'user', None)}")
        return sess

    async def _ensure_voice(self, sess: _Session, v: _Voice, d: dict) -> None:
        client = sess.client
        guild = client.get_guild(v.guild_id)
        if guild is None:
            v.state, v.code = "error", "not_in_guild"
            v.error = "This radio bot is not a member of that Discord server — invite it first."
            return
        channel = guild.get_channel(v.channel_id)
        if channel is None or getattr(channel, "type", None) not in _VOICE_TYPES:
            v.state, v.code = "error", "bad_channel"
            v.error = "That channel is not a voice channel the radio bot can see."
            return
        vc = v.vc
        if vc is not None and vc.is_connected() and getattr(vc.channel, "id", None) == v.channel_id:
            if not vc.is_playing():
                self._play(v)
            v.state, v.error, v.code = "connected", None, None
            return
        refusal = _join_refusal(guild, channel)
        if refusal:
            if v.error != refusal[1]:   # once per change, not every watchdog pass
                logger.error(f"Stream {v.stream_id}: {refusal[1]}")
            v.state, v.code, v.error = "error", refusal[0], refusal[1]
            return
        v.state = "connecting"
        try:
            existing = guild.voice_client
            if existing is not None and existing.is_connected():
                await existing.move_to(channel)
                v.vc = existing
            else:
                if existing is not None:
                    await existing.disconnect(force=True)
                v.vc = await channel.connect()
        except asyncio.TimeoutError:
            # discord.py's TimeoutError has no message, so the old text read
            # "(TimeoutError: )" — useless on the admin page.
            v.vc = None
            v.state, v.code = "error", "connect_timeout"
            # Permissions were checked above, so this is usually the voice
            # handshake itself (e.g. a pre-2.7 discord.py refused with 4017 for
            # lacking DAVE E2EE, then retried until the 30s timeout) or UDP egress.
            v.error = ("Discord never completed the voice join (timed out). Permissions look fine, so "
                       "check the node's discord.py voice log (DAVE close 4017, UDP blocked).")
            logger.error(f"Stream {v.stream_id}: voice connect timed out")
            return
        except Exception as e:
            v.vc = None
            v.state, v.code = "error", "connect_failed"
            v.error = f"Could not connect to the voice channel ({type(e).__name__}: {str(e)[:120]})."
            logger.error(f"Stream {v.stream_id}: voice connect failed: {type(e).__name__}: {e}")
            return
        # Bounded wait for the shared PulseAudio socket (op25 container creates it).
        if not await pulse.wait_until_ready():
            v.state, v.code = "error", "no_audio"
            v.error = "The node's audio path (PulseAudio) is not up."
            return
        self._play(v, pulse_checked=True)
        v.state, v.error, v.code = "connected", None, None
        logger.info(f"Stream {v.stream_id} connected (bot {v.token_id})")

    async def _sync_presence(self, sess: _Session, target: dict[str, dict]) -> None:
        """Show "Listening to <system>" only while this bot is really in voice.

        It used to be set once on the first successful join and never cleared,
        so a bot whose voice later dropped (or kept failing to rejoin) still
        looked healthy in Discord's member list.
        """
        name = None
        for sid, v in sorted(self._voices.items()):
            if (v.token_id == sess.token_id and v.state == "connected"
                    and v.vc is not None and v.vc.is_connected()):
                name = (target.get(sid) or {}).get("system_name") or name
                if name:
                    break
        if sess.presence == name:
            return
        try:
            activity = discord.Activity(type=discord.ActivityType.listening, name=name) if name else None
            await sess.client.change_presence(activity=activity)
            sess.presence = name
        except Exception as e:
            logger.warning(f"Presence update failed: {e}")

    # ------------------------------------------------------------------
    # Audio
    # ------------------------------------------------------------------

    def _play(self, v: _Voice, pulse_checked: bool = False) -> None:
        vc = v.vc
        if vc is None or not vc.is_connected():
            return
        if not pulse_checked and not pulse.is_ready():
            logger.error(f"PulseAudio socket {pulse.socket_path()} missing — retrying audio in {STREAM_RETRY_DELAY}s.")
            self._schedule_replay(v.stream_id)
            return
        try:
            vc.play(self._audio_factory(), after=lambda err, sid=v.stream_id: self._on_audio_end(sid, err))
        except Exception as e:
            logger.error(f"Stream {v.stream_id}: could not start audio: {e}")
            self._schedule_replay(v.stream_id)

    def _on_audio_end(self, stream_id: str, error) -> None:
        """discord.py calls this from its player thread."""
        if error:
            logger.error(f"Stream {stream_id} audio ended with error: {error}")
        self._schedule_replay(stream_id, STREAM_RETRY_DELAY if error else 0.5)

    def _schedule_replay(self, stream_id: str, delay: float = STREAM_RETRY_DELAY) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return

        async def _later():
            await asyncio.sleep(delay)
            v = self._voices.get(stream_id)
            if v and v.vc and v.vc.is_connected() and not v.vc.is_playing():
                self._play(v)

        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_later()))

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    async def _disconnect(self, v: _Voice) -> None:
        vc, v.vc = v.vc, None
        if vc is None:
            return
        try:
            if vc.is_playing():
                vc.stop()
            await vc.disconnect(force=True)
        except Exception as e:
            logger.warning(f"Stream {v.stream_id}: voice disconnect error: {e}")

    async def _close_session(self, token_id: str) -> None:
        sess = self._sessions.pop(token_id, None)
        if sess is None:
            return
        for v in self._voices.values():
            if v.token_id == token_id:
                await self._disconnect(v)
        await self._shutdown_client(sess)
        logger.info(f"Radio bot {token_id} logged out")

    async def _shutdown_client(self, sess: _Session) -> None:
        try:
            await sess.client.close()
        except Exception as e:
            logger.warning(f"Radio bot {sess.token_id} close error: {e}")
        if sess.task and not sess.task.done():
            sess.task.cancel()
            try:
                await sess.task
            except BaseException:
                pass

    async def stop(self) -> None:
        """Node shutdown: log every radio bot out."""
        if self._watchdog:
            self._watchdog.cancel()
            self._watchdog = None
        async with self._lock:
            for v in list(self._voices.values()):
                await self._disconnect(v)
            self._voices.clear()
            for tid in list(self._sessions):
                await self._close_session(tid)
            self._tokens.clear()

    # ------------------------------------------------------------------
    # Watchdog + MQTT-loss fail-safe
    # ------------------------------------------------------------------

    def _ensure_watchdog(self) -> None:
        self._loop = asyncio.get_running_loop()
        if self._watchdog is None or self._watchdog.done():
            self._watchdog = asyncio.create_task(self._watchdog_loop())

    async def _watchdog_loop(self) -> None:
        while True:
            await asyncio.sleep(WATCHDOG_INTERVAL)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Discord watchdog error: {e}")

    async def tick(self, now: Optional[float] = None) -> None:
        """One watchdog pass: fail-safe check, then reconcile + report if needed."""
        now = time.monotonic() if now is None else now
        before = self.snapshot()
        if self._mqtt_up():
            self._mqtt_down_since = None
            if self._failsafe:
                logger.info("MQTT back — resuming Discord radio streams.")
                self._failsafe = False
        else:
            if self._mqtt_down_since is None:
                self._mqtt_down_since = now
            elif (not self._failsafe and self._sessions
                  and now - self._mqtt_down_since >= settings.discord_mqtt_loss_stop_seconds):
                logger.warning("MQTT unreachable past the fail-safe — logging every radio bot out "
                               "(C2 will hand these tokens to other nodes).")
                self._failsafe = True
        await self.reconcile()
        self._ticks += 1
        # On change, and every few passes regardless: a periodic report is what
        # lets C2 settle a release whose first ack was lost.
        if self.snapshot() != before or self._ticks % REPORT_EVERY_TICKS == 0:
            await self.publish_actual()

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def snapshot(self) -> dict:
        streams = {}
        for sid in self._desired:
            v = self._voices.get(sid)
            if self._paused:
                streams[sid] = {"state": "paused_locally", "error": None, "code": None}
            elif v is None:
                streams[sid] = {"state": "connecting", "error": None, "code": None}
            else:
                streams[sid] = {"state": v.state, "error": v.error, "code": v.code}
        return {
            "applied_seq": self._applied_seq,
            "sessions": sorted(tid for tid, s in self._sessions.items() if s.alive()),
            "streams": streams,
        }

    async def publish_actual(self) -> None:
        try:
            await self._publish(self.snapshot())
        except Exception as e:
            logger.warning(f"Publishing Discord actual state failed: {e}")

    # metadata_watcher hooks — audio plays continuously, nothing to do per call.
    def start_stream(self):
        pass

    def stop_stream(self):
        pass

    @property
    def is_connected(self) -> bool:
        return any(v.vc is not None and v.vc.is_connected() for v in self._voices.values())


radio_bot = RadioManager()
