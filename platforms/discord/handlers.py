"""Discord bot handlers for Claude Code bridge.

Handles messages and interactions, forwarding to Claude sessions.
Key difference from Telegram: channel = project directory (no folder picker).
"""

import asyncio
import logging
import os
import tempfile
from typing import Optional

import discord

from config import DISCORD_ALLOWED_GUILDS, DISCORD_CHANNEL_PROJECTS
from session import sessions, start_session_discord, send_to_claude, resolve_permission, interrupt_session
from commands import get_command_prompt, get_help_message

logger = logging.getLogger("tele-claude.discord.handlers")


def is_authorized_guild(guild_id: Optional[int]) -> bool:
    """Check if a guild is authorized to use the bot."""
    if not DISCORD_ALLOWED_GUILDS:
        return True  # No restrictions
    if guild_id is None:
        return False
    return guild_id in DISCORD_ALLOWED_GUILDS


def get_project_for_channel(channel_id: int) -> Optional[str]:
    """Get project directory for a channel from config.
    
    Returns None if channel is not mapped to a project.
    """
    return DISCORD_CHANNEL_PROJECTS.get(channel_id)


async def handle_message(message: discord.Message, bot: discord.Client) -> None:
    """Handle incoming Discord message."""
    # Ignore bot messages
    if message.author.bot:
        return
    
    # Authorization check
    guild_id = message.guild.id if message.guild else None
    if not is_authorized_guild(guild_id):
        logger.warning(f"Unauthorized message from guild {guild_id}")
        return
    
    channel_id = message.channel.id
    text = message.content
    
    # Check if this channel has an active session
    if channel_id in sessions:
        session = sessions[channel_id]
        
        # Check for slash commands
        prompt = text
        if text.startswith("/"):
            command_part = text.split()[0]
            command_name = command_part.lstrip("/")
            
            cmd_prompt = get_command_prompt(command_name, session.contextual_commands)
            if cmd_prompt is not None:
                prompt = cmd_prompt
                logger.debug(f"Executing slash command /{command_name}")
        
        # Handle pending image
        pending_image = session.pending_image_path
        if pending_image:
            session.pending_image_path = None
            prompt = f"{pending_image}\n\n{prompt}"
        
        # Interrupt ongoing query
        was_interrupted = await interrupt_session(channel_id)
        if was_interrupted:
            await asyncio.sleep(0.1)
        
        # Run as background task
        asyncio.create_task(send_to_claude(channel_id, prompt, None))
        return
    
    # Check if channel is mapped to a project
    project_path = get_project_for_channel(channel_id)
    if project_path:
        # Auto-start session for this channel
        success = await start_session_discord(channel_id, project_path, message.channel)
        if success:
            # Now send the message to the new session
            asyncio.create_task(send_to_claude(channel_id, text, None))
        else:
            await message.channel.send(f"Failed to start session: project '{project_path}' not found")


async def handle_attachment(message: discord.Message, bot: discord.Client) -> None:
    """Handle message with image attachment."""
    if message.author.bot:
        return
    
    guild_id = message.guild.id if message.guild else None
    if not is_authorized_guild(guild_id):
        return
    
    channel_id = message.channel.id
    if channel_id not in sessions:
        return
    
    session = sessions[channel_id]
    
    # Find image attachment
    image_attachment = None
    for attachment in message.attachments:
        if attachment.content_type and attachment.content_type.startswith('image/'):
            image_attachment = attachment
            break
    
    if not image_attachment:
        return
    
    # Download image to temp directory
    from pathlib import Path
    image_path = Path(tempfile.gettempdir()) / f"discord_image_{image_attachment.id}.png"
    await image_attachment.save(image_path)
    image_path = str(image_path)  # Convert back to str for session
    
    caption = message.content
    if caption:
        # Image with caption - send immediately
        prompt = f"{image_path}\n\n{caption}"
        was_interrupted = await interrupt_session(channel_id)
        if was_interrupted:
            await asyncio.sleep(0.1)
        asyncio.create_task(send_to_claude(channel_id, prompt, None))
    else:
        # Image without caption - buffer for next text message
        session.pending_image_path = image_path


async def handle_interaction(interaction: discord.Interaction) -> None:
    """Handle button interactions (permission responses)."""
    if not interaction.data:
        return
    
    custom_id = interaction.data.get("custom_id", "")
    
    # Handle permission responses: "perm:<action>:<request_id>:<tool_name>"
    if custom_id.startswith("perm:"):
        parts = custom_id.split(":", 3)
        if len(parts) != 4:
            await interaction.response.send_message("Invalid permission callback", ephemeral=True)
            return
        
        _, action, request_id, tool_name = parts
        
        # Get session for logging
        channel_id = interaction.channel_id
        session = sessions.get(channel_id) if channel_id else None
        
        if session and session.logger:
            session.logger.log_permission_callback(request_id, action, tool_name)
        
        if action == "allow":
            success = await resolve_permission(request_id, allowed=True, always=False, tool_name=tool_name)
            if success:
                await interaction.response.edit_message(content=f"✅ Allowed `{tool_name}` (one-time)", view=None)
            else:
                await interaction.response.edit_message(content="⚠️ Permission request expired", view=None)
        
        elif action == "deny":
            success = await resolve_permission(request_id, allowed=False, always=False, tool_name=tool_name)
            if success:
                await interaction.response.edit_message(content=f"❌ Denied `{tool_name}`", view=None)
            else:
                await interaction.response.edit_message(content="⚠️ Permission request expired", view=None)
        
        elif action == "always":
            success = await resolve_permission(request_id, allowed=True, always=True, tool_name=tool_name)
            if success:
                await interaction.response.edit_message(content=f"✅ Always allowed `{tool_name}`", view=None)
            else:
                await interaction.response.edit_message(content="⚠️ Permission request expired", view=None)


async def handle_help(message: discord.Message) -> None:
    """Handle /help command."""
    channel_id = message.channel.id
    
    contextual_commands: list = []
    if channel_id in sessions:
        contextual_commands = sessions[channel_id].contextual_commands
    
    help_text = get_help_message(contextual_commands)
    # Discord uses markdown, so strip HTML and use plain markdown
    # The help message is already markdown-friendly
    await message.channel.send(help_text)
