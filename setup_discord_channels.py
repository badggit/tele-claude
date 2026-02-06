#!/usr/bin/env python3
"""Create Discord channels for each project folder."""

import asyncio
import discord
from pathlib import Path

from config import DISCORD_BOT_TOKEN, PROJECTS_DIR

async def main():
    intents = discord.Intents.default()
    client = discord.Client(intents=intents)
    
    @client.event
    async def on_ready():
        print(f"Bot: {client.user}")
        
        if not client.guilds:
            print("Error: Bot not in any servers!")
            await client.close()
            return
        
        guild = client.guilds[0]
        print(f"Server: {guild.name}")
        
        # Get existing channel names
        existing = {ch.name for ch in guild.text_channels}
        print(f"Existing channels: {existing}")
        
        # Get project folders
        projects = [p for p in PROJECTS_DIR.iterdir() if p.is_dir() and not p.name.startswith('.')]
        print(f"\nFound {len(projects)} projects in {PROJECTS_DIR}")
        
        # Create channels for each project
        for project in sorted(projects):
            # Sanitize name for Discord (lowercase, no spaces, max 100 chars)
            channel_name = project.name.lower().replace(' ', '-').replace('_', '-')[:100]

            if channel_name in existing:
                print(f"  ✓ #{channel_name} exists")
            else:
                try:
                    await guild.create_text_channel(channel_name)
                    print(f"  + Created #{channel_name}")
                except Exception as e:
                    print(f"  ✗ Failed to create #{channel_name}: {e}")
        
        await client.close()
    
    if not DISCORD_BOT_TOKEN:
        raise ValueError("DISCORD_BOT_TOKEN not found in environment")
    await client.start(DISCORD_BOT_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
