"""Discord platform client implementation.

Wraps discord.py's interactions to implement PlatformClient protocol.
"""

import asyncio
import time
from io import BytesIO
from typing import Optional, Protocol, runtime_checkable

import discord

from ..protocol import ButtonRow, ButtonSpec, MessageRef, PlatformClient
from .formatter import DiscordFormatter, split_text


# Rate limiting constants
MIN_SEND_INTERVAL = 0.5  # Discord is more lenient than Telegram
TYPING_ACTION_INTERVAL = 8.0  # Discord typing lasts ~10 seconds


@runtime_checkable
class ErrorLogger(Protocol):
    """Protocol for error logging capability."""

    def log_error(self, context: str, error: Exception) -> None:
        """Log an error with context."""
        ...


class DiscordClient(PlatformClient):
    """Discord implementation of PlatformClient protocol.
    
    Handles:
    - Message sending/editing
    - Typing indicators
    - File/image sending
    - Button interactions via Views
    """

    def __init__(
        self,
        channel: discord.TextChannel,
        logger: Optional[ErrorLogger] = None,
    ):
        """Initialize Discord client.
        
        Args:
            channel: Discord channel to send messages to
            logger: Optional SessionLogger for error logging
        """
        self._channel = channel
        self._logger = logger
        self._formatter = DiscordFormatter()
        
        # Rate limiting state
        self._last_send = 0.0
        self._send_interval = MIN_SEND_INTERVAL
        self._last_typing = 0.0

    @property
    def max_message_length(self) -> int:
        return 2000  # Discord's limit

    @property
    def channel(self) -> discord.TextChannel:
        """Access underlying Discord channel."""
        return self._channel

    def _build_view(self, buttons: Optional[list[ButtonRow]]) -> Optional[discord.ui.View]:
        """Convert ButtonRows to Discord View with buttons."""
        if not buttons:
            return None
        
        view = discord.ui.View(timeout=None)  # No timeout for permission buttons
        
        for row_idx, row in enumerate(buttons):
            for btn in row.buttons:
                button = discord.ui.Button(
                    label=btn.text,
                    custom_id=btn.callback_id,
                    row=row_idx,
                )
                view.add_item(button)
        
        return view

    async def _apply_rate_limit(self) -> None:
        """Wait if necessary to respect rate limits."""
        now = time.time()
        elapsed = now - self._last_send
        if elapsed < self._send_interval:
            await asyncio.sleep(self._send_interval - elapsed)

    def _log_error(self, context: str, error: Exception) -> None:
        """Log error if logger is available."""
        if self._logger:
            self._logger.log_error(context, error)

    async def send_message(
        self,
        text: str,
        *,
        buttons: Optional[list[ButtonRow]] = None,
    ) -> MessageRef:
        """Send a new message with optional buttons."""
        if not text.strip():
            return MessageRef(platform_data=None)

        await self._apply_rate_limit()

        chunks = split_text(text, self.max_message_length)
        view = self._build_view(buttons)
        msg = None

        for i, chunk in enumerate(chunks):
            # Only add view to last chunk
            is_last = i == len(chunks) - 1
            try:
                if is_last and view:
                    msg = await self._channel.send(content=chunk, view=view)
                else:
                    msg = await self._channel.send(content=chunk)
                self._last_send = time.time()
            except Exception as e:
                self._log_error("send_message", e)

        return MessageRef(platform_data=msg)

    async def edit_message(
        self,
        ref: MessageRef,
        text: str,
        *,
        buttons: Optional[list[ButtonRow]] = None,
    ) -> None:
        """Edit an existing message."""
        msg: Optional[discord.Message] = ref.platform_data
        if not msg:
            return

        # Truncate if too long
        if len(text) > self.max_message_length:
            text = text[:self.max_message_length - 10] + "\n..."

        view = self._build_view(buttons)

        try:
            await msg.edit(content=text, view=view)
        except discord.NotFound:
            # Message was deleted
            pass
        except Exception as e:
            self._log_error("edit_message", e)

    async def delete_message(self, ref: MessageRef) -> None:
        """Delete a message."""
        msg: Optional[discord.Message] = ref.platform_data
        if not msg:
            return

        try:
            await msg.delete()
        except discord.NotFound:
            pass  # Already deleted
        except Exception as e:
            self._log_error("delete_message", e)

    async def send_typing(self) -> None:
        """Send typing indicator if enough time has passed."""
        now = time.time()
        if now - self._last_typing < TYPING_ACTION_INTERVAL:
            return

        try:
            await self._channel.typing()
            self._last_typing = now
        except Exception as e:
            self._log_error("send_typing", e)

    async def send_photo(
        self,
        image: BytesIO,
        caption: Optional[str] = None,
    ) -> MessageRef:
        """Send a single image."""
        try:
            image.seek(0)
            file = discord.File(image, filename="image.png")
            msg = await self._channel.send(content=caption, file=file)
            return MessageRef(platform_data=msg)
        except Exception as e:
            self._log_error("send_photo", e)
            return MessageRef(platform_data=None)

    async def send_photos(
        self,
        images: list[tuple[BytesIO, str]],
    ) -> None:
        """Send multiple images."""
        if not images:
            return

        try:
            files = []
            for i, (img_buffer, caption) in enumerate(images):
                img_buffer.seek(0)
                files.append(discord.File(img_buffer, filename=f"image_{i}.png"))
            
            # Send all images in one message, with first caption as content
            content = images[0][1] if images else None
            await self._channel.send(content=content, files=files)
        except Exception as e:
            self._log_error("send_photos", e)

    async def send_document(
        self,
        path: str,
        caption: Optional[str] = None,
    ) -> MessageRef:
        """Send a file/document."""
        try:
            file = discord.File(path)
            msg = await self._channel.send(content=caption, file=file)
            return MessageRef(platform_data=msg)
        except Exception as e:
            self._log_error("send_document", e)
            return MessageRef(platform_data=None)
