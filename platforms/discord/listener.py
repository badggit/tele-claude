from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import discord

from config import DISCORD_ALLOWED_GUILDS
from core.dispatcher import TransportListener
from core.types import Trigger, make_session_key
from utils import ensure_image_within_limits
from .reply_target import DiscordReplyTarget
from .handlers import resolve_project_for_channel, _is_general_channel

_log = logging.getLogger("tele-claude.discord.listener")


class DiscordListener(TransportListener):
    """Listens for Discord messages and converts to Triggers."""

    platform = "discord"

    def __init__(self, bot_token: str, allowed_guilds: set[int] = DISCORD_ALLOWED_GUILDS) -> None:
        self._bot_token = bot_token
        self._allowed_guilds = allowed_guilds
        self._on_trigger: Optional[Callable[[Trigger], Awaitable[None]]] = None
        self._client: Optional[discord.Client] = None
        self._task: Optional[asyncio.Task] = None

    def resolve_cwd(self, trigger: Trigger) -> Optional[str]:
        return trigger.reply_context.get("cwd")

    async def start(self, on_trigger: Callable[[Trigger], Awaitable[None]]) -> None:
        self._on_trigger = on_trigger

        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True

        self._client = _DiscordClient(self, intents=intents)
        self._task = asyncio.create_task(self._client.start(self._bot_token))

    async def stop(self) -> None:
        if self._client:
            await self._client.close()
        if self._task:
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def create_reply_target(self, reply_context: dict[str, Any]) -> DiscordReplyTarget:
        if not self._client:
            raise RuntimeError("DiscordListener not started")
        channel = reply_context.get("channel")
        channel_id = reply_context.get("channel_id")
        return DiscordReplyTarget(client=self._client, channel=channel, channel_id=channel_id)

    async def create_session(self, trigger: Trigger, cwd: str) -> Any:
        import session as session_module

        if not self._client:
            raise RuntimeError("DiscordListener not started")

        reply_context = trigger.reply_context
        channel = reply_context.get("channel")
        channel_id = reply_context.get("channel_id")
        if channel is None and channel_id is not None:
            channel = self._client.get_channel(channel_id)
            if channel is None:
                channel = await self._client.fetch_channel(channel_id)
        if channel is None:
            raise RuntimeError("Discord channel not available")

        display_name = "~" if cwd == str(Path.home()) else Path(cwd).name
        success = await session_module.start_session_discord(
            channel_id=channel.id,
            project_path=cwd,
            channel=channel,
            display_name=display_name,
        )
        if not success:
            raise RuntimeError("Failed to start Discord session")
        return session_module.sessions[channel.id]

    def _is_authorized_guild(self, guild_id: Optional[int]) -> bool:
        if not self._allowed_guilds:
            return True
        if guild_id is None:
            return False
        return guild_id in self._allowed_guilds

    async def _handle_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        guild_id = message.guild.id if message.guild else None
        if not self._is_authorized_guild(guild_id):
            return
        if not self._on_trigger:
            return

        has_image = any(
            a.content_type and a.content_type.startswith("image/")
            for a in message.attachments
        )
        images: list[str] = []
        if has_image:
            images = await self._download_images(message)

        channel = message.channel
        if isinstance(channel, discord.Thread):
            cwd = None
            parent_name = channel.parent.name if channel.parent else None
            if parent_name:
                cwd = resolve_project_for_channel(parent_name)
            if cwd is None and _is_general_channel(channel):
                cwd = str(Path.home())

            trigger = Trigger(
                platform="discord",
                session_key=make_session_key("discord", channel_id=channel.id),
                prompt=message.content or "",
                images=images,
                reply_context={
                    "channel": channel,
                    "channel_id": channel.id,
                    "cwd": cwd,
                },
                source="user",
            )
            await self._on_trigger(trigger)
            return

        if isinstance(channel, discord.TextChannel):
            cwd = resolve_project_for_channel(channel.name)
            if cwd is None and _is_general_channel(channel):
                cwd = str(Path.home())
            if cwd is None:
                return

            thread_name = (message.content or "Claude session")[:100]
            try:
                thread = await message.create_thread(name=thread_name)
            except Exception:
                _log.exception("Failed to create Discord thread")
                return

            trigger = Trigger(
                platform="discord",
                session_key=make_session_key("discord", channel_id=thread.id),
                prompt=message.content or "",
                images=images,
                reply_context={
                    "channel": thread,
                    "channel_id": thread.id,
                    "cwd": cwd,
                },
                source="user",
            )
            await self._on_trigger(trigger)

    async def _download_images(self, message: discord.Message) -> list[str]:
        images: list[str] = []
        for attachment in message.attachments:
            if not attachment.content_type or not attachment.content_type.startswith("image/"):
                continue
            suffix = os.path.splitext(attachment.filename)[1] or ".jpg"
            temp_path = Path(tempfile.gettempdir()) / f"discord_{attachment.id}{suffix}"
            await attachment.save(temp_path)
            result_path = await asyncio.to_thread(ensure_image_within_limits, str(temp_path))
            images.append(result_path)
        return images

    async def _handle_interaction(self, interaction: discord.Interaction) -> None:
        from platforms.discord.handlers import handle_interaction

        await handle_interaction(interaction)


class _DiscordClient(discord.Client):
    def __init__(self, listener: DiscordListener, **kwargs) -> None:
        super().__init__(**kwargs)
        self._listener = listener

    async def on_message(self, message: discord.Message) -> None:
        await self._listener._handle_message(message)

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        await self._listener._handle_interaction(interaction)
