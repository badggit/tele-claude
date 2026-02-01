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

from config import DISCORD_BOT_TOKEN, DISCORD_CHANNEL_PROJECTS
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

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._watchdog_task = None

    async def setup_hook(self):
        """Called after login, before READY. Start the watchdog."""
        self._watchdog_task = self.loop.create_task(self._event_loop_watchdog())

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

    async def on_ready(self):
        _log.info(f"Discord bot logged in as {self.user}")
        _log.info(f"Configured channel mappings: {DISCORD_CHANNEL_PROJECTS}")

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

    if not DISCORD_CHANNEL_PROJECTS:
        _log.warning("No DISCORD_CHANNEL_PROJECTS configured - bot won't auto-start sessions")

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
