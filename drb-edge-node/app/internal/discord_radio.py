import asyncio
from typing import Optional
import discord
from discord.ext import commands
from app.config import settings
from app.internal import pulse
from app.internal.logger import logger

BOT_READY_TIMEOUT = 15   # seconds to wait for Discord bot to become ready
WATCHDOG_INTERVAL = 30   # seconds between voice-connection health checks
REJOIN_DELAY = 5  # seconds to wait before attempting a rejoin
STREAM_RETRY_DELAY = 5   # seconds to back off before re-arming the audio source


class RadioBot:
    def __init__(self):
        self._bot: Optional[commands.Bot] = None
        self._voice_client: Optional[discord.VoiceClient] = None
        self._task: Optional[asyncio.Task] = None
        self._watchdog_task: Optional[asyncio.Task] = None
        self._ready_event: Optional[asyncio.Event] = None
        self._current_token: Optional[str] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Remembered so we can rejoin after an unexpected disconnect
        self._guild_id: Optional[int] = None
        self._channel_id: Optional[int] = None
        self._was_streaming: bool = False

    async def join(self, guild_id: int, channel_id: int, token: str, call_active: bool = False, system_name: str = None) -> bool:
        # (Re)start the bot if the token changed or the bot isn't running
        if self._current_token != token or not self._is_bot_running():
            if not await self._start_bot(token):
                return False

        guild = self._bot.get_guild(guild_id)
        if not guild:
            logger.error(f"Guild {guild_id} not found — bot may not be a member.")
            return False

        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            logger.error(f"Channel {channel_id} is not a voice channel.")
            return False

        try:
            if self._voice_client and self._voice_client.is_connected():
                await self._voice_client.disconnect(force=True)
            self._voice_client = await channel.connect()
            # Remember where we are so the watchdog can rejoin if we drop
            self._guild_id = guild_id
            self._channel_id = channel_id
            # Bounded wait for the shared PulseAudio socket. Historically FFmpeg was
            # launched before the op25 container had created it, failed instantly,
            # and the bot sat silently connected forever.
            await pulse.wait_until_ready()
            self._play_stream()
            if system_name:
                await self._bot.change_presence(
                    activity=discord.Activity(
                        type=discord.ActivityType.listening,
                        name=system_name,
                    )
                )
            logger.info(f"Joined #{channel.name} in {guild.name}")
            return True
        except Exception as e:
            logger.error(f"Failed to join voice channel: {e}")
            return False

    async def leave(self) -> bool:
        # Clear remembered channel so the watchdog doesn't rejoin
        self._guild_id = None
        self._channel_id = None
        self._was_streaming = False

        if self._voice_client and self._voice_client.is_connected():
            try:
                self._stop_stream()
                await self._voice_client.disconnect(force=True)
                self._voice_client = None
                await self._bot.change_presence(activity=None)
                logger.info("Disconnected from voice channel.")
                return True
            except Exception as e:
                logger.error(f"Failed to disconnect: {e}")
        return False

    def start_stream(self):
        """Called when an OP25 call starts — stream plays continuously, nothing to do."""
        pass

    def stop_stream(self):
        """Called when an OP25 call ends — stream plays continuously, nothing to do."""
        pass

    async def stop(self):
        self._guild_id = None
        self._channel_id = None
        self._was_streaming = False

        if self._watchdog_task:
            self._watchdog_task.cancel()
            self._watchdog_task = None

        await self.leave()

        if self._task:
            self._task.cancel()
        if self._bot:
            await self._bot.close()
        self._bot = None
        self._task = None
        self._current_token = None
        self._ready_event = None

    def _play_stream(self):
        """
        Feed Discord voice straight from the PulseAudio monitor.

        Icecast is NOT used here: it lags ~1 s at connect and drifts to 100 s+,
        which is unusable for live listening. Icecast remains the frontend/mobile
        listening path only.
        """
        if not self._voice_client:
            return

        if not pulse.is_ready():
            logger.error(
                f"PulseAudio socket {pulse.socket_path()} missing — "
                f"cannot start Discord audio; retrying in {STREAM_RETRY_DELAY}s."
            )
            self._schedule_restart()
            return

        # before_options land ahead of -i, so this becomes:
        #   ffmpeg -f pulse -i drb_sink.monitor …
        source = discord.FFmpegPCMAudio(
            settings.pulse_source,
            before_options="-f pulse",
        )
        self._voice_client.play(
            discord.PCMVolumeTransformer(source, volume=1.0),
            after=self._on_stream_end,
        )

    def _schedule_restart(self, delay: float = STREAM_RETRY_DELAY):
        """Re-arm the audio source after a delay — safe to call from any thread."""
        if not self._loop:
            return

        async def _delayed_restart():
            await asyncio.sleep(delay)
            vc = self._voice_client
            if vc and vc.is_connected() and not vc.is_playing():
                self._play_stream()

        self._loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_delayed_restart()))

    def _on_stream_end(self, error):
        if error:
            logger.error(f"Stream ended with error: {error}")
        vc = self._voice_client
        if not (self._loop and vc and vc.is_connected() and not vc.is_playing()):
            return
        if error:
            # Back off before retrying — prevents a tight loop when PulseAudio is
            # unavailable (FFmpeg exits immediately in that case).
            self._schedule_restart()
        else:
            self._loop.call_soon_threadsafe(self._play_stream)

    def _stop_stream(self):
        if self._voice_client and self._voice_client.is_playing():
            self._voice_client.stop()

    async def _watchdog_loop(self):
        """Periodically verify the voice connection is alive; rejoin silently if not."""
        await asyncio.sleep(WATCHDOG_INTERVAL)  # give initial join time to settle
        while True:
            try:
                await asyncio.sleep(WATCHDOG_INTERVAL)

                # Only act if we're supposed to be in a channel
                if not self._guild_id or not self._channel_id:
                    continue

                connected = self._voice_client is not None and self._voice_client.is_connected()
                if not connected:
                    logger.warning("Watchdog: voice connection lost — attempting rejoin.")
                    await asyncio.sleep(REJOIN_DELAY)
                    rejoined = await self.join(
                        self._guild_id,
                        self._channel_id,
                        self._current_token,
                    )
                    if rejoined:
                        logger.info("Watchdog: successfully rejoined voice channel.")
                    else:
                        logger.error("Watchdog: rejoin failed — will retry next cycle.")

            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error(f"Watchdog error: {e}")

    async def _start_bot(self, token: str) -> bool:
        await self.stop()  # clean up any previous instance

        intents = discord.Intents.default()
        intents.voice_states = True
        intents.message_content = True
        intents.messages = True
        self._bot = commands.Bot(command_prefix="!", intents=intents)
        self._ready_event = asyncio.Event()
        self._current_token = token
        self._loop = asyncio.get_event_loop()

        @self._bot.event
        async def on_ready():
            logger.info(f"Discord bot ready: {self._bot.user} ({self._bot.user.id})")
            self._ready_event.set()

        @self._bot.event
        async def on_voice_state_update(
            member: discord.Member,
            before: discord.VoiceState,
            after: discord.VoiceState,
        ):
            """Detect when our own bot gets disconnected from a voice channel."""
            if self._bot.user and member.id != self._bot.user.id:
                return
            if before.channel is not None and after.channel is None:
                # Bot was disconnected (kicked or server drop).
                # Don't null _voice_client here — Discord.py reconnects voice
                # transparently and would leave _voice_client stale as None.
                # The watchdog detects real disconnects via is_connected().
                logger.warning("Bot was disconnected from voice channel — watchdog will rejoin if needed.")

        @self._bot.event
        async def on_message(message: discord.Message):
            if message.author.bot:
                return
            if self._bot.user not in message.mentions:
                return
            content = message.content.lower()
            if "leave" in content:
                await self.leave()
                try:
                    await message.reply("Disconnected.")
                except Exception:
                    pass
            elif "joinme" in content or "join" in content:
                member = message.guild.get_member(message.author.id) if message.guild else None
                vc = member.voice.channel if member and member.voice else None
                if not vc:
                    try:
                        await message.reply("You're not in a voice channel.")
                    except Exception:
                        pass
                    return
                try:
                    if self._voice_client and self._voice_client.is_connected():
                        await self._voice_client.move_to(vc)
                    else:
                        self._voice_client = await vc.connect()
                    self._channel_id = vc.id
                    # A fresh connect() has no audio source attached yet.
                    if not self._voice_client.is_playing():
                        self._play_stream()
                    await message.reply(f"Joined {vc.name}.")
                except Exception as e:
                    logger.error(f"joinme failed: {e}")

        self._task = asyncio.create_task(self._bot.start(token))

        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=BOT_READY_TIMEOUT)
            self._watchdog_task = asyncio.create_task(self._watchdog_loop())
            return True
        except asyncio.TimeoutError:
            logger.error("Timed out waiting for Discord bot to become ready.")
            await self.stop()
            return False

    def _is_bot_running(self) -> bool:
        return (
            self._bot is not None
            and self._task is not None
            and not self._task.done()
        )

    @property
    def is_connected(self) -> bool:
        return self._voice_client is not None and self._voice_client.is_connected()


radio_bot = RadioBot()
