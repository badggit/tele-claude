#!/usr/bin/env python3
"""Discord bot entry point for Claude Code bridge.

Usage:
  python run_discord.py

Requires DISCORD_BOT_TOKEN in .env file.
Configure DISCORD_CHANNEL_PROJECTS to map channels to project directories.
"""

import asyncio
import logging

import discord
from discord import Intents

from config import DISCORD_BOT_TOKEN, DISCORD_CHANNEL_PROJECTS
from platforms.discord.handlers import handle_message, handle_attachment, handle_interaction
from logger import setup_logging

# Configure logging
setup_logging()
logger = logging.getLogger("tele-claude.discord")


class ClaudeBotClient(discord.Client):
    """Discord client for Claude Code bridge."""

    async def on_ready(self):
        logger.info(f"Discord bot logged in as {self.user}")
        logger.info(f"Configured channel mappings: {DISCORD_CHANNEL_PROJECTS}")

    async def on_message(self, message: discord.Message):
        # Ignore bot messages
        if message.author.bot:
            return

        # Handle attachments (images)
        if message.attachments:
            await handle_attachment(message, self)
            if not message.content:  # Image only, no text
                return

        # Handle text messages
        if message.content:
            await handle_message(message, self)

    async def on_interaction(self, interaction: discord.Interaction):
        await handle_interaction(interaction)


def main():
    if not DISCORD_BOT_TOKEN:
        raise ValueError("DISCORD_BOT_TOKEN not found in environment")

    if not DISCORD_CHANNEL_PROJECTS:
        logger.warning("No DISCORD_CHANNEL_PROJECTS configured - bot won't auto-start sessions")

    # Set up intents
    intents = Intents.default()
    intents.message_content = True  # Required for reading message content
    intents.guilds = True

    client = ClaudeBotClient(intents=intents)
    client.run(DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    main()
