"""
Discord bot runner.
"""
import logging

import discord
from discord import Intents

from config import DISCORD_BOT_TOKEN, DISCORD_CHANNEL_PROJECTS
from platforms.discord.handlers import handle_message, handle_attachment, handle_interaction
from logger import setup_logging


class ClaudeBotClient(discord.Client):
    """Discord client for Claude Code bridge."""

    async def on_ready(self):
        logger = logging.getLogger("tele-claude.discord")
        logger.info(f"Discord bot logged in as {self.user}")
        logger.info(f"Configured channel mappings: {DISCORD_CHANNEL_PROJECTS}")

    async def on_message(self, message: discord.Message):
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
    logger = logging.getLogger("tele-claude.discord")

    if not DISCORD_BOT_TOKEN:
        raise ValueError("DISCORD_BOT_TOKEN not found in environment")

    if not DISCORD_CHANNEL_PROJECTS:
        logger.warning("No DISCORD_CHANNEL_PROJECTS configured - bot won't auto-start sessions")

    # Set up intents
    intents = Intents.default()
    intents.message_content = True
    intents.guilds = True

    client = ClaudeBotClient(intents=intents)
    client.run(DISCORD_BOT_TOKEN)
