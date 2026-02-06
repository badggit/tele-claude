"""
Discord bot runner.
"""
import asyncio
import json
import logging
import sys
import time

import discord
from discord import Intents

from config import DISCORD_BOT_TOKEN
from platforms.discord.handlers import handle_message, handle_attachment, handle_interaction
from logger import setup_logging

_log = logging.getLogger("tele-claude.discord")
_gateway_log = logging.getLogger("tele-claude.discord.gateway")

# Also log to stderr so we see critical events in real-time
_stderr_handler = logging.StreamHandler(sys.stderr)
_stderr_handler.setLevel(logging.WARNING)
_stderr_handler.setFormatter(logging.Formatter(
    '[%(asctime)s] [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S'
))
logging.getLogger("tele-claude").addHandler(_stderr_handler)


class ClaudeBotClient(discord.Client):
    """Discord client for Claude Code bridge."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._watchdog_task: asyncio.Task[None] | None = None
        self._task_factory_registered: bool = False
        self._defer_task_factory: bool = False

    async def setup_hook(self) -> None:
        """Called after login, before READY. Start the watchdog."""
        self._watchdog_task = self.loop.create_task(self._event_loop_watchdog())
        from task_api import start_task_api

        await start_task_api()
        if self.guilds:
            await self._register_task_factory()
        else:
            self._defer_task_factory = True

    async def _register_task_factory(self) -> None:
        if self._task_factory_registered:
            return

        from task_api import register_task_channel_factory
        from session import start_session_ambient_discord

        bot_client = self

        async def create_discord_task_channel(task_name: str) -> int:
            tasks_channel = None
            for guild in bot_client.guilds:
                for channel in guild.text_channels:
                    if channel.name == "tasks":
                        tasks_channel = channel
                        break
                if tasks_channel:
                    break

            if not tasks_channel:
                raise RuntimeError("No #tasks channel found in any authorized guild")

            thread = await tasks_channel.create_thread(
                name=task_name,
                type=discord.ChannelType.public_thread,
            )
            await start_session_ambient_discord(thread.id, thread)
            return thread.id

        register_task_channel_factory(create_discord_task_channel)
        self._task_factory_registered = True

    async def on_connect(self):
        _log.info("Discord gateway connected")

    async def on_disconnect(self):
        _log.warning("Discord gateway disconnected")

    async def on_resumed(self):
        _log.info("Discord gateway resumed")

    async def on_socket_event_type(self, event_type: str):
        """Log every gateway DISPATCH event type."""
        _gateway_log.debug("event type=%s", event_type)

    async def on_error(self, event_method: str, *args, **kwargs):
        _log.exception("Discord event error in %s", event_method)

    async def on_socket_raw_receive(self, msg: str):
        """Log every raw gateway event (op/t)."""
        try:
            data = json.loads(msg)
            op = data.get("op")
            t = data.get("t")
            s = data.get("s")
            if t == "MESSAGE_CREATE":
                d = data.get("d") or {}
                author = d.get("author") or {}
                _gateway_log.debug(
                    "recv op=%s t=%s s=%s id=%s channel_id=%s author_id=%s",
                    op, t, s, d.get("id"), d.get("channel_id"), author.get("id")
                )
            else:
                _gateway_log.debug("recv op=%s t=%s s=%s", op, t, s)
        except Exception:
            _gateway_log.debug("recv raw len=%d", len(msg))

    async def on_socket_raw_send(self, msg: str):
        """Log outgoing gateway payloads (op/t)."""
        try:
            data = json.loads(msg)
            op = data.get("op")
            t = data.get("t")
            _gateway_log.debug("send op=%s t=%s", op, t)
        except Exception:
            _gateway_log.debug("send raw len=%d", len(msg))

    async def _event_loop_watchdog(self):
        """Periodic heartbeat to detect event loop blocks.

        Logs every 60 seconds. If the gap between logs is much larger than 60s,
        it means the event loop was blocked during that interval.
        """
        logger = logging.getLogger("tele-claude.watchdog")
        last_beat = time.monotonic()
        while True:
            await asyncio.sleep(60)
            now = time.monotonic()
            elapsed = now - last_beat
            last_beat = now

            # Normal: ~60s. If >90s, something delayed us.
            if elapsed > 90:
                logger.warning(
                    "Event loop was blocked! Expected 60s heartbeat, got %.1fs (%.1fs late)",
                    elapsed, elapsed - 60
                )
            else:
                logger.debug("Watchdog heartbeat: %.1fs (ok)", elapsed)

    async def on_ready(self) -> None:
        _log.info(f"Discord bot logged in as {self.user}")
        if self._defer_task_factory and not self._task_factory_registered:
            self._defer_task_factory = False
            await self._register_task_factory()

    async def on_message(self, message: discord.Message):
        # Log all messages (including bot's own) for debugging
        _log.info(
            "on_message: author=%s channel=%s channel_id=%s message_id=%s content=%s",
            message.author,
            message.channel,
            message.channel.id,
            message.id,
            message.content[:80] if message.content else '(empty)'
        )

        # Ignore bot messages
        if message.author.bot:
            return

        # Check for image attachments
        has_image = any(
            a.content_type and a.content_type.startswith('image/')
            for a in message.attachments
        )

        if has_image:
            await handle_attachment(message, self)
            return

        # Handle text-only messages
        if message.content:
            await handle_message(message, self)

    async def on_interaction(self, interaction: discord.Interaction):
        await handle_interaction(interaction)


def run() -> None:
    """Run Discord bot.

    Uses DISCORD_BOT_TOKEN from environment/.env file.
    """
    setup_logging()

    if not DISCORD_BOT_TOKEN:
        raise ValueError("DISCORD_BOT_TOKEN not found in environment")

    # Set up intents
    intents = Intents.default()
    intents.message_content = True
    intents.guilds = True

    # Log Discord HTTP rate limits at DEBUG level (goes to app.log)
    http_logger = logging.getLogger("discord.http")
    http_logger.setLevel(logging.DEBUG)

    client = ClaudeBotClient(
        intents=intents,
        max_ratelimit_timeout=30.0,  # Raise RateLimited instead of silently sleeping >30s
        enable_debug_events=True,  # Enable raw gateway receive/send events
    )
    client.run(DISCORD_BOT_TOKEN)
