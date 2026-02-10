"""Discord bot handlers for Claude Code bridge.

Handles messages and interactions, forwarding to Claude sessions.
Model: channel = project, thread = conversation (like Telegram forum topics).
"""

import asyncio
import time
import logging
import tempfile
from typing import Optional

import discord

from dispatcher import dispatcher, DispatchItem
from config import DISCORD_ALLOWED_GUILDS, PROJECTS_DIR, AVAILABLE_MODELS, CLAUDE_MODEL
from session import sessions, start_session_discord, start_session_ambient_discord, start_claude_task, resolve_permission, interrupt_session, stop_session
from utils import ensure_image_within_limits
from commands import get_command_prompt, get_help_message

logger = logging.getLogger("tele-claude.discord.handlers")


def is_authorized_guild(guild_id: Optional[int]) -> bool:
    """Check if a guild is authorized to use the bot."""
    if not DISCORD_ALLOWED_GUILDS:
        return True  # No restrictions
    if guild_id is None:
        return False
    return guild_id in DISCORD_ALLOWED_GUILDS


def _normalize_name(name: str) -> str:
    """Normalize a name for matching: lowercase, replace _ and spaces with -."""
    return name.lower().replace("_", "-").replace(" ", "-")


def resolve_project_for_channel(channel_name: str) -> Optional[str]:
    """Resolve a project directory by matching channel name to a PROJECTS_DIR subfolder.

    Returns the full path as string, or None if no match.
    """
    if not PROJECTS_DIR.exists():
        return None
    normalized = _normalize_name(channel_name)
    for d in PROJECTS_DIR.iterdir():
        if d.is_dir() and not d.name.startswith(".") and _normalize_name(d.name) == normalized:
            return str(d)
    return None


def _is_general_channel(channel: discord.abc.Messageable) -> bool:
    """Check if channel is #general (ambient channel for home folder sessions)."""
    # Get the actual channel (parent if thread)
    if isinstance(channel, discord.Thread):
        parent = channel.parent
        if parent:
            return parent.name.lower() == "general"
        return False
    if isinstance(channel, discord.TextChannel):
        return channel.name.lower() == "general"
    return False


async def _send_queue_full_notice(message: discord.Message) -> None:
    try:
        await message.reply("Bot is busy. Please retry in a moment.")
    except Exception as e:
        logger.warning("Failed to send queue full notice: %s", e)


async def handle_message(message: discord.Message, bot: discord.Client) -> None:
    """Enqueue incoming Discord message for processing."""
    # Ignore bot messages
    if message.author.bot:
        return

    # Authorization check
    guild_id = message.guild.id if message.guild else None
    if not is_authorized_guild(guild_id):
        logger.warning(f"Unauthorized message from guild {guild_id}")
        return

    session_id = message.channel.id
    dispatcher.enqueue(DispatchItem(
        name="discord.message",
        session_id=session_id,
        coro=lambda: _handle_message_impl(message, bot),
        on_drop=lambda: _send_queue_full_notice(message),
    ))


async def _handle_message_impl(message: discord.Message, bot: discord.Client) -> None:
    """Handle incoming Discord message.

    If message is in a project channel: create a thread and start session there.
    If message is in a thread: continue session in that thread.
    """
    text = message.content
    channel = message.channel

    # Determine if we're in a thread or a channel
    if isinstance(channel, discord.Thread):
        # Message is in a thread - use thread ID as session key
        thread_id = channel.id
        parent_channel_name = channel.parent.name if channel.parent else None

        # Check if this thread has an active session
        if thread_id in sessions:
            session = sessions[thread_id]

            # Check for slash commands
            prompt = text
            if text.startswith("/"):
                command_part = text.split()[0]
                command_name = command_part.lstrip("/")

                # Handle built-in /help command
                if command_name == "help":
                    await handle_help(message)
                    return

                # Handle /model command
                if command_name == "model":
                    await handle_model(message)
                    return

                # Handle /close command - stop session and archive thread
                if command_name == "close":
                    await handle_close(message)
                    return

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
            t0 = time.perf_counter()
            was_interrupted = await interrupt_session(thread_id)
            if session.logger:
                session.logger.log_debug(
                    "interrupt",
                    "interrupt_session completed (message)",
                    thread_id=thread_id,
                    interrupted=was_interrupted,
                    elapsed_ms=int((time.perf_counter() - t0) * 1000),
                )
            if was_interrupted:
                await asyncio.sleep(0.1)

            # Run as background task
            start_claude_task(thread_id, prompt, None)
            return

        # No session in this thread - check if parent channel is a project channel or #general
        command_name = None
        if text.startswith("/"):
            command_name = text.split()[0].lstrip("/")

        project_path = resolve_project_for_channel(parent_channel_name) if parent_channel_name else None
        if project_path:
            # Start new session in this thread
            success = await start_session_discord(thread_id, project_path, channel)
            if success:
                if command_name == "model":
                    session = sessions.get(thread_id)
                    if session:
                        await _handle_model_command(text, session, channel)
                else:
                    start_claude_task(thread_id, text, None)
            else:
                await channel.send(f"Failed to start session: project not found")
        elif _is_general_channel(channel):
            # Start ambient session for #general thread
            success = await start_session_ambient_discord(thread_id, channel)
            if success:
                if command_name == "model":
                    session = sessions.get(thread_id)
                    if session:
                        await _handle_model_command(text, session, channel)
                else:
                    start_claude_task(thread_id, text, None)
            else:
                await channel.send(f"Failed to start ambient session")
        return

    # Message is in a channel (not a thread)
    project_path = resolve_project_for_channel(channel.name) if isinstance(channel, discord.TextChannel) else None

    if project_path:
        # Create a new thread for this conversation
        thread_name = text[:100] if text else "Claude session"
        try:
            thread = await message.create_thread(name=thread_name)
            logger.info(f"Created thread '{thread_name}' for project {project_path}")

            # Start session in the new thread
            success = await start_session_discord(thread.id, project_path, thread)
            if success:
                start_claude_task(thread.id, text, None)
            else:
                await thread.send(f"Failed to start session: project not found")
        except Exception as e:
            logger.error(f"Failed to create thread: {e}")
            await message.reply(f"Failed to create thread: {e}")

    elif _is_general_channel(channel):
        # #general channel - create thread and start ambient session
        thread_name = text[:100] if text else "Claude session"
        try:
            thread = await message.create_thread(name=thread_name)
            logger.info(f"Created thread '{thread_name}' in #general (ambient)")

            success = await start_session_ambient_discord(thread.id, thread)
            if success:
                start_claude_task(thread.id, text, None)
            else:
                await thread.send(f"Failed to start ambient session")
        except Exception as e:
            logger.error(f"Failed to create thread: {e}")
            await message.reply(f"Failed to create thread: {e}")


async def handle_attachment(message: discord.Message, bot: discord.Client) -> None:
    """Enqueue image attachment handling."""
    if message.author.bot:
        return

    guild_id = message.guild.id if message.guild else None
    if not is_authorized_guild(guild_id):
        return

    session_id = message.channel.id
    dispatcher.enqueue(DispatchItem(
        name="discord.attachment",
        session_id=session_id,
        coro=lambda: _handle_attachment_impl(message, bot),
        on_drop=lambda: _send_queue_full_notice(message),
    ))


async def _handle_attachment_impl(message: discord.Message, bot: discord.Client) -> None:
    """Handle message with image attachment."""
    channel = message.channel

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
    image_path = str(image_path)

    # Resize if needed to prevent API errors (max 2000px for multi-image requests)
    image_path = await asyncio.to_thread(ensure_image_within_limits, image_path)

    caption = message.content or "What's in this image?"

    # If in a channel, create a thread first
    if not isinstance(channel, discord.Thread):
        project_path = resolve_project_for_channel(channel.name) if isinstance(channel, discord.TextChannel) else None

        if project_path:
            thread_name = caption[:100] if caption else "Image analysis"
            try:
                thread = await message.create_thread(name=thread_name)
                logger.info(f"Created thread '{thread_name}' for image in {project_path}")

                success = await start_session_discord(thread.id, project_path, thread)
                if success:
                    prompt = f"{image_path}\n\n{caption}"
                    start_claude_task(thread.id, prompt, None)
                else:
                    await thread.send("Failed to start session: project not found")
            except Exception as e:
                logger.error(f"Failed to create thread for image: {e}")
                await message.reply(f"Failed to create thread: {e}")

        elif _is_general_channel(channel):
            # #general channel - create thread and start ambient session for image
            thread_name = caption[:100] if caption else "Image analysis"
            try:
                thread = await message.create_thread(name=thread_name)
                logger.info(f"Created thread '{thread_name}' for image in #general (ambient)")

                success = await start_session_ambient_discord(thread.id, thread)
                if success:
                    prompt = f"{image_path}\n\n{caption}"
                    start_claude_task(thread.id, prompt, None)
                else:
                    await thread.send("Failed to start ambient session")
            except Exception as e:
                logger.error(f"Failed to create thread for image: {e}")
                await message.reply(f"Failed to create thread: {e}")
        return

    # In a thread - use existing session or start new one
    thread_id = channel.id
    parent_channel_name = channel.parent.name if channel.parent else None

    if thread_id not in sessions:
        # Start session if parent is a project channel or #general
        project_path = resolve_project_for_channel(parent_channel_name) if parent_channel_name else None
        if project_path:
            success = await start_session_discord(thread_id, project_path, channel)
            if not success:
                await channel.send("Failed to start session: project not found")
        elif _is_general_channel(channel):
            success = await start_session_ambient_discord(thread_id, channel)
            if not success:
                await channel.send("Failed to start ambient session")
                return
        else:
            return

    session = sessions[thread_id]
    prompt = f"{image_path}\n\n{caption}"

    t0 = time.perf_counter()
    was_interrupted = await interrupt_session(thread_id)
    if session.logger:
        session.logger.log_debug(
            "interrupt",
            "interrupt_session completed (attachment)",
            thread_id=thread_id,
            interrupted=was_interrupted,
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
        )
    if was_interrupted:
        await asyncio.sleep(0.1)

    start_claude_task(thread_id, prompt, None)


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

        # Get session for logging - check thread ID first, then channel
        channel = interaction.channel
        session_id = channel.id if channel else None
        session = sessions.get(session_id) if session_id else None

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
    channel = message.channel
    session_id = channel.id

    contextual_commands: list = []
    current_model: str | None = None
    if session_id in sessions:
        session = sessions[session_id]
        contextual_commands = session.contextual_commands
        current_model = session.model_override or CLAUDE_MODEL

    help_text = get_help_message(contextual_commands, current_model)
    await channel.send(help_text)


async def _handle_model_command(
    text: str,
    session,
    channel: discord.abc.Messageable,
) -> None:
    """Handle /model command logic for a specific session."""
    args = text.split(maxsplit=1)
    if len(args) < 2:
        current = session.model_override or CLAUDE_MODEL or "default"
        models_list = "\n".join(f"  `{m}`" for m in AVAILABLE_MODELS)
        await channel.send(
            f"**Current model:** `{current}`\n\n**Available:**\n{models_list}\n\nUsage: /model <name>"
        )
        return

    model_name = args[1].strip()
    if model_name not in AVAILABLE_MODELS:
        models_list = "\n".join(f"  `{m}`" for m in AVAILABLE_MODELS)
        await channel.send(f"Unknown model: `{model_name}`\n\n**Available:**\n{models_list}")
        return

    session.model_override = model_name
    await channel.send(f"Model switched to `{model_name}` for this session.")


async def handle_model(message: discord.Message) -> None:
    """Handle /model command - switch model for the current session."""
    channel = message.channel
    session_id = channel.id

    if session_id not in sessions:
        await channel.send("No active session. Start a session first.")
        return

    session = sessions[session_id]
    await _handle_model_command(message.content, session, channel)


async def handle_close(message: discord.Message) -> None:
    """Handle /close command - stop session and archive thread."""
    channel = message.channel

    if not isinstance(channel, discord.Thread):
        await channel.send("This command only works in threads.")
        return

    thread_id = channel.id

    # Stop the Claude session if it exists
    if thread_id in sessions:
        await stop_session(thread_id)
        await channel.send("Session closed.")

    # Archive the thread
    try:
        await channel.edit(archived=True)
        logger.info(f"Archived thread {thread_id}")
    except Exception as e:
        logger.error(f"Failed to archive thread: {e}")
        await channel.send(f"Failed to archive thread: {e}")
