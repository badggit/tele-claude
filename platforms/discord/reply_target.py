from __future__ import annotations

from typing import Optional

import discord

from core.types import ReplyCapabilities, ReplyTarget
from platforms.protocol import ButtonRow, MessageRef, PlatformMessage
from .client import DiscordClient


class DiscordReplyTarget(ReplyTarget):
    """ReplyTarget wrapper around DiscordClient with lazy channel resolution."""

    def __init__(
        self,
        *,
        client: discord.Client,
        channel: Optional[discord.abc.Messageable] = None,
        channel_id: Optional[int] = None,
    ) -> None:
        self._client = client
        self._channel = channel
        self._channel_id = channel_id
        self._platform: Optional[DiscordClient] = None

    @property
    def capabilities(self) -> ReplyCapabilities:
        # Defaults from DiscordClient
        return ReplyCapabilities(
            can_edit=True,
            can_buttons=True,
            can_typing=True,
            max_length=2000,
            max_buttons_per_row=5,
        )

    async def _ensure_platform(self) -> DiscordClient:
        if self._platform:
            return self._platform

        channel = self._channel
        if channel is None and self._channel_id is not None:
            channel = self._client.get_channel(self._channel_id)
            if channel is None:
                channel = await self._client.fetch_channel(self._channel_id)
        if channel is None:
            raise RuntimeError("DiscordReplyTarget missing channel")

        self._channel = channel
        self._platform = DiscordClient(channel=channel)  # type: ignore[arg-type]
        return self._platform

    async def send(self, message: PlatformMessage) -> MessageRef:
        platform = await self._ensure_platform()
        return await platform.send_message(message)

    async def edit(self, ref: MessageRef, message: PlatformMessage) -> None:
        platform = await self._ensure_platform()
        await platform.edit_message(ref, message)

    async def send_buttons(self, message: PlatformMessage, buttons: list[ButtonRow]) -> MessageRef:
        platform = await self._ensure_platform()
        return await platform.send_message(message, buttons=buttons)

    async def typing(self) -> None:
        platform = await self._ensure_platform()
        await platform.send_typing()
