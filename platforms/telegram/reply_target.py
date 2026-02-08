from __future__ import annotations

from typing import Optional

from core.types import ReplyCapabilities, ReplyTarget
from platforms.protocol import ButtonRow, MessageRef, PlatformMessage
from .client import TelegramClient


class TelegramReplyTarget(ReplyTarget):
    """ReplyTarget wrapper around TelegramClient."""

    def __init__(self, client: TelegramClient):
        self._client = client

    @property
    def capabilities(self) -> ReplyCapabilities:
        return self._client.capabilities

    async def send(self, message: PlatformMessage) -> MessageRef:
        return await self._client.send_message(message)

    async def edit(self, ref: MessageRef, message: PlatformMessage) -> None:
        await self._client.edit_message(ref, message)

    async def send_buttons(self, message: PlatformMessage, buttons: list[ButtonRow]) -> MessageRef:
        return await self._client.send_message(message, buttons=buttons)

    async def typing(self) -> None:
        await self._client.send_typing()
