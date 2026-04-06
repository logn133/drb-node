import asyncio
from typing import Optional
import discord
from discord.ext import commands
from app.config import settings
from app.internal.logger import logger

BOT_READY_TIMEOUT = 15  # seconds to wait for Discord bot to become ready


class RadioBot:
    def __init__(self):
        self._bot: Optional[commands.Bot] = None
        self._voice_client: Optional[discord.VoiceClient] = None
        self._task: Optional[asyncio.Task] = None
        self._ready_event: Optional[asyncio.Event] = None
        self._current_token: Optional[str] = None
        self._icecast_url = (
            f"http://{settings.icecast_host}:{settings.icecast_port}{settings.icecast_mount}"
        )

    async def join(self, guild_id: int, channel_id: int, token: str, call_active: bool = False) -> bool:
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
            # Only start playing immediately if a call is currently active
            if call_active:
                self._play_stream()
            logger.info(f"Joined #{channel.name} in {guild.name} (streaming={'yes' if call_active else 'waiting for call'})")
            return True
        except Exception as e:
            logger.error(f"Failed to join voice channel: {e}")
            return False

    async def leave(self) -> bool:
        if self._voice_client and self._voice_client.is_connected():
            try:
                self._stop_stream()
                await self._voice_client.disconnect(force=True)
                self._voice_client = None
                logger.info("Disconnected from voice channel.")
                return True
            except Exception as e:
                logger.error(f"Failed to disconnect: {e}")
        return False

    def start_stream(self):
        """Called when an OP25 call starts — begin transmitting audio and light the ring."""
        if self._voice_client and self._voice_client.is_connected():
            if not self._voice_client.is_playing():
                self._play_stream()
                logger.debug("Stream started (call active).")

    def stop_stream(self):
        """Called when an OP25 call ends — stop transmitting so the ring goes dark."""
        if self._voice_client and self._voice_client.is_connected():
            self._stop_stream()
            logger.debug("Stream stopped (call ended).")

    async def stop(self):
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
        if not self._voice_client:
            return
        source = discord.FFmpegPCMAudio(
            self._icecast_url,
            before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
        )
        self._voice_client.play(
            discord.PCMVolumeTransformer(source, volume=1.0),
            after=lambda e: logger.error(f"Stream ended unexpectedly: {e}") if e else None,
        )

    def _stop_stream(self):
        if self._voice_client and self._voice_client.is_playing():
            self._voice_client.stop()

    async def _start_bot(self, token: str) -> bool:
        await self.stop()  # clean up any previous instance

        intents = discord.Intents.default()
        intents.voice_states = True
        intents.message_content = True
        intents.messages = True
        self._bot = commands.Bot(command_prefix="!", intents=intents)
        self._ready_event = asyncio.Event()
        self._current_token = token

        @self._bot.event
        async def on_ready():
            logger.info(f"Discord bot ready: {self._bot.user} ({self._bot.user.id})")
            self._ready_event.set()

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
                    await message.reply(f"Joined {vc.name}.")
                except Exception as e:
                    logger.error(f"joinme failed: {e}")

        self._task = asyncio.create_task(self._bot.start(token))

        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=BOT_READY_TIMEOUT)
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
