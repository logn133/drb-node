"""
Edge Discord radio manager (node-26#25): desired-state reconcile, logout on
release, watchdog that survives a dead session, MQTT-loss fail-safe.

Discord itself is faked: FakeClient logs "in" by firing on_ready, guilds and
channels are plain objects, voice connections record play/disconnect.
"""
import asyncio

import discord
import pytest

from app.internal import discord_radio
from app.internal.discord_radio import RadioManager


class FakeVC:
    def __init__(self, channel):
        self.channel = channel
        self.connected = True
        self.playing = False
        self.plays = 0

    def is_connected(self):
        return self.connected

    def is_playing(self):
        return self.playing

    def play(self, source, after=None):
        self.playing = True
        self.plays += 1

    def stop(self):
        self.playing = False

    async def disconnect(self, force=False):
        self.connected = False
        self.playing = False
        self.channel.guild.voice_client = None

    async def move_to(self, channel):
        self.channel = channel
        channel.guild.voice_client = self


class FakeChannel:
    def __init__(self, guild, cid, kind=discord.ChannelType.voice):
        self.guild, self.id, self.type = guild, cid, kind
        self.perms = discord.Permissions(view_channel=True, connect=True, speak=True)
        self.user_limit = 0
        self.members = []
        self.connect_error = None

    def permissions_for(self, member):
        assert member is self.guild.me
        return self.perms

    async def connect(self):
        if self.connect_error:
            raise self.connect_error
        vc = FakeVC(self)
        self.guild.voice_client = vc
        return vc


class FakeGuild:
    def __init__(self, gid, channel_ids):
        self.id = gid
        self.me = object()
        self.voice_client = None
        self.channels = {c: FakeChannel(self, c) for c in channel_ids}

    def get_channel(self, cid):
        return self.channels.get(cid)


class FakeClient:
    instances: list = []

    def __init__(self, guilds, fail_login=False):
        self.guilds = guilds
        self.fail_login = fail_login
        self.closed = False
        self.handlers = {}
        self.token = None
        self.user = "radio-bot"
        FakeClient.instances.append(self)

    def event(self, coro):
        self.handlers[coro.__name__] = coro
        return coro

    async def start(self, token):
        self.token = token
        if self.fail_login:
            raise discord.LoginFailure("Improper token has been passed.")
        await self.handlers["on_ready"]()
        while not self.closed:
            await asyncio.sleep(0.01)

    async def close(self):
        self.closed = True

    def is_closed(self):
        return self.closed

    def get_guild(self, gid):
        return self.guilds.get(gid)

    async def change_presence(self, activity=None):
        self.presence = activity


@pytest.fixture
def env(monkeypatch):
    FakeClient.instances = []
    guilds = {1: FakeGuild(1, [11, 12]), 2: FakeGuild(2, [21])}
    published = []
    fetched = []
    state = {"mqtt": True, "fail_login": False}

    async def fetch(token_id):
        fetched.append(token_id)
        return f"secret-{token_id}"

    async def publish(payload):
        published.append(payload)

    async def pulse_ready(timeout=None):
        return True
    monkeypatch.setattr(discord_radio.pulse, "wait_until_ready", pulse_ready)
    monkeypatch.setattr(discord_radio.pulse, "is_ready", lambda: True)

    mgr = RadioManager(
        client_factory=lambda: FakeClient(guilds, fail_login=state["fail_login"]),
        token_fetcher=fetch, publisher=publish, audio_factory=lambda: object(),
        mqtt_up=lambda: state["mqtt"], ready_timeout=1,
    )
    yield type("Env", (), {"mgr": mgr, "guilds": guilds, "published": published,
                           "fetched": fetched, "state": state})
    # Leave no background tasks behind.
    if mgr._watchdog:
        mgr._watchdog.cancel()


def _desired(seq, *streams):
    return {"seq": seq, "streams": [
        {"stream_id": f"{g}:{c}", "guild_id": str(g), "channel_id": str(c), "token_id": t, "system_name": "County"}
        for g, c, t in streams
    ]}


async def test_apply_connects_and_reports(env):
    await env.mgr.apply_desired(_desired(1, (1, 11, "t1")))
    actual = env.published[-1]
    assert actual["applied_seq"] == 1
    assert actual["sessions"] == ["t1"]
    assert actual["streams"] == {"1:11": {"state": "connected", "error": None, "code": None}}
    assert env.mgr.is_connected
    vc = env.guilds[1].voice_client
    assert vc.plays == 1 and vc.channel.id == 11
    assert FakeClient.instances[0].presence.name == "County"


async def test_one_session_serves_two_guilds(env):
    await env.mgr.apply_desired(_desired(1, (1, 11, "t1"), (2, 21, "t1")))
    assert len(FakeClient.instances) == 1
    assert env.published[-1]["sessions"] == ["t1"]
    assert env.guilds[1].voice_client.is_connected() and env.guilds[2].voice_client.is_connected()


async def test_dropping_the_last_stream_logs_the_bot_out(env):
    """node-26#25 item 1: leave() used to keep the gateway session alive."""
    await env.mgr.apply_desired(_desired(1, (1, 11, "t1"), (2, 21, "t1")))
    client = FakeClient.instances[0]
    await env.mgr.apply_desired(_desired(2, (2, 21, "t1")))
    assert not client.closed                      # still needed for guild 2
    assert env.guilds[1].voice_client is None
    await env.mgr.apply_desired(_desired(3))
    assert client.closed                          # logged OUT, not just out of voice
    assert env.published[-1] == {"applied_seq": 3, "sessions": [], "streams": {}}


async def test_stale_desired_is_ignored(env):
    await env.mgr.apply_desired(_desired(5, (1, 11, "t1")))
    await env.mgr.apply_desired(_desired(4))
    assert env.mgr.snapshot()["applied_seq"] == 5 and env.mgr.is_connected


async def test_dead_session_is_recovered_by_the_watchdog(env):
    """node-26#25 item 2: the old watchdog cancelled itself on rejoin."""
    await env.mgr.apply_desired(_desired(1, (1, 11, "t1")))
    first = FakeClient.instances[0]
    first.closed = True                           # bot task dies
    await asyncio.sleep(0.05)
    env.guilds[1].voice_client.connected = False
    await env.mgr.tick()
    assert len(FakeClient.instances) == 2 and not FakeClient.instances[1].closed
    assert env.mgr.snapshot()["streams"]["1:11"]["state"] == "connected"
    # The watchdog task itself is still alive after the recovery.
    assert env.mgr._watchdog is not None and not env.mgr._watchdog.done()


async def test_dropped_voice_is_reconnected(env):
    await env.mgr.apply_desired(_desired(1, (1, 11, "t1")))
    env.guilds[1].voice_client.connected = False
    env.guilds[1].voice_client = None
    await env.mgr.tick()
    assert env.guilds[1].voice_client.is_connected()


async def test_bot_not_in_guild_is_reported_not_swallowed(env):
    await env.mgr.apply_desired(_desired(1, (9, 91, "t1")))
    st = env.published[-1]["streams"]["9:91"]
    assert st["state"] == "error" and st["code"] == "not_in_guild"


async def test_login_failure_is_reported(env):
    env.state["fail_login"] = True
    await env.mgr.apply_desired(_desired(1, (1, 11, "t1")))
    st = env.published[-1]["streams"]["1:11"]
    assert st["state"] == "error" and st["code"] == "login_failed"
    assert env.published[-1]["sessions"] == []


async def test_mqtt_loss_failsafe_logs_out_then_resumes(env, monkeypatch):
    monkeypatch.setattr(discord_radio.settings, "discord_mqtt_loss_stop_seconds", 300)
    await env.mgr.apply_desired(_desired(1, (1, 11, "t1")))
    client = FakeClient.instances[0]
    env.state["mqtt"] = False
    await env.mgr.tick(now=1000.0)
    assert not client.closed                       # inside the window
    await env.mgr.tick(now=1299.0)
    assert not client.closed
    await env.mgr.tick(now=1300.0)
    assert client.closed and env.mgr.snapshot()["sessions"] == []
    # Token strings are not kept across a fail-safe logout: re-login refetches
    # from C2, which refuses if the lease has moved on.
    assert env.mgr._tokens == {}
    env.state["mqtt"] = True
    before = len(env.fetched)
    await env.mgr.tick(now=1400.0)
    assert len(env.fetched) == before + 1
    assert env.mgr.snapshot()["streams"]["1:11"]["state"] == "connected"


async def test_token_fetch_refused_reports_error(env):
    async def refuse(token_id):
        return None
    env.mgr._fetch_token = refuse
    await env.mgr.apply_desired(_desired(1, (1, 11, "t1")))
    st = env.published[-1]["streams"]["1:11"]
    assert st["state"] == "error" and FakeClient.instances == []


async def test_local_pause_logs_out_until_next_desired(env):
    await env.mgr.apply_desired(_desired(1, (1, 11, "t1")))
    await env.mgr.pause_locally()
    assert FakeClient.instances[0].closed
    assert env.published[-1]["streams"]["1:11"]["state"] == "paused_locally"
    await env.mgr.apply_desired(_desired(2, (1, 11, "t1")))
    assert env.mgr.is_connected


async def test_no_message_content_intent_on_real_client():
    client = discord_radio._default_client_factory()
    try:
        assert client.intents.message_content is False
        assert client.intents.voice_states and client.intents.guilds
    finally:
        await client.close()


def _presence(client):
    activity = getattr(client, "presence", None)
    return activity.name if activity else None


async def test_missing_connect_permission_is_named_not_a_timeout(env):
    """Discord ignores a join the bot may not make; say which permission is missing."""
    env.guilds[1].channels[11].perms = discord.Permissions(view_channel=True, speak=True)
    await env.mgr.apply_desired(_desired(1, (1, 11, "t1")))
    st = env.published[-1]["streams"]["1:11"]
    assert st["state"] == "error" and st["code"] == "missing_permissions"
    assert "Connect" in st["error"] and "View Channel" not in st["error"]
    assert env.guilds[1].voice_client is None
    assert _presence(FakeClient.instances[0]) is None   # not "Listening to …" while out of voice


async def test_full_channel_is_reported(env):
    ch = env.guilds[1].channels[11]
    ch.user_limit, ch.members = 2, [object(), object()]
    await env.mgr.apply_desired(_desired(1, (1, 11, "t1")))
    st = env.published[-1]["streams"]["1:11"]
    assert st["code"] == "channel_full" and "2/2" in st["error"]


async def test_bots_own_lingering_voice_state_does_not_fill_the_channel(env):
    """A dropped connection can leave the bot listed in the channel; that is
    not a slot it needs to rejoin."""
    guild = env.guilds[1]
    guild.me = type("Me", (), {"id": 999})()
    ch = guild.channels[11]
    ch.user_limit, ch.members = 2, [object(), guild.me]
    await env.mgr.apply_desired(_desired(1, (1, 11, "t1")))
    assert env.published[-1]["streams"]["1:11"]["state"] == "connected"


async def test_connect_timeout_has_a_readable_error(env):
    env.guilds[1].channels[11].connect_error = asyncio.TimeoutError()
    await env.mgr.apply_desired(_desired(1, (1, 11, "t1")))
    st = env.published[-1]["streams"]["1:11"]
    assert st["state"] == "error" and st["code"] == "connect_timeout"
    assert "timed out" in st["error"] and "TimeoutError: )" not in st["error"]


async def test_presence_is_cleared_when_voice_is_lost_and_rejoin_fails(env):
    """The owner saw "Listening to <system>" on a bot that was not in voice:
    presence was set once on join and never cleared."""
    await env.mgr.apply_desired(_desired(1, (1, 11, "t1")))
    client = FakeClient.instances[0]
    assert _presence(client) == "County"
    env.guilds[1].voice_client.connected = False
    env.guilds[1].voice_client = None
    env.guilds[1].channels[11].connect_error = RuntimeError("voice handshake failed")
    await env.mgr.tick()
    st = env.mgr.snapshot()["streams"]["1:11"]
    assert st["state"] == "error" and st["code"] == "connect_failed"
    assert _presence(client) is None
    env.guilds[1].channels[11].connect_error = None
    await env.mgr.tick()
    assert _presence(client) == "County"
