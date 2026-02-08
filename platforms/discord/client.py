"""Discord platform client implementation.

Wraps discord.py's interactions to implement PlatformClient protocol.

IMPORTANT: Never wrap discord.py API calls in asyncio.wait_for().
discord.py manages its own rate limit state with internal locks and events.
Cancelling a request mid-flight (via wait_for timeout) can corrupt that state,
leaving locks permanently held and deadlocking the entire HTTP pipeline.
Let discord.py handle timeouts natively via max_ratelimit_timeout on the Client.
"""

import asyncio
import logging
import time
from io import BytesIO
from typing import Optional, Protocol, runtime_checkable

import discord

from core.types import ReplyCapabilities
from ..protocol import (
    ButtonRow,
    MessageRef,
    PlatformClient,
    PlatformMessage,
    TextMessage,
    ToolCallMessage,
    ThinkingMessage,
)
from .formatter import DiscordFormatter, split_text

_log = logging.getLogger("tele-claude.discord.client")

# Rate limiting constants
MIN_SEND_INTERVAL = 1.0  # Minimum seconds between sends (prevent burst)
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
    def capabilities(self) -> ReplyCapabilities:
        return ReplyCapabilities(
            can_edit=True,
            can_buttons=True,
            can_typing=True,
            max_length=self.max_message_length,
            max_buttons_per_row=5,
        )

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
        _log.warning("Discord API error [%s]: %s: %s", context, type(error).__name__, error)

    def _render_tool_call(self, message: ToolCallMessage) -> str:
        """Render tool call(s) to Discord markdown."""
        if not message.calls:
            return self._formatter.format_tool_calls_batch(message.tool_name, [])
        if len(message.calls) == 1:
            name, args = message.calls[0]
            return self._formatter.format_tool_call(name, args)
        return self._formatter.format_tool_calls_batch(message.tool_name, message.calls)

    def _render_message(self, message: PlatformMessage) -> str:
        """Render a platform message to Discord markdown."""
        if isinstance(message, TextMessage):
            return self._formatter.format_markdown(message.text)
        if isinstance(message, ToolCallMessage):
            return self._render_tool_call(message)
        if isinstance(message, ThinkingMessage):
            return message.text
        raise TypeError(f"Unsupported message type: {type(message).__name__}")

    def _build_thinking_embed(self, text: str) -> discord.Embed:
        """Build a thinking embed with safe text."""
        safe_text = self._formatter.escape_text(text)
        max_len = 4000
        if len(safe_text) > max_len:
            safe_text = safe_text[:max_len - 3] + "..."
        return discord.Embed(
            description=f"🧠 {safe_text}",
            color=discord.Color.greyple(),
        )

    async def send_message(
        self,
        message: PlatformMessage,
        *,
        buttons: Optional[list[ButtonRow]] = None,
    ) -> MessageRef:
        """Send a new message with optional buttons."""
        if isinstance(message, TextMessage):
            if not message.text.strip():
                return MessageRef(platform_data=None)
        elif isinstance(message, ThinkingMessage):
            if not message.text.strip():
                return MessageRef(platform_data=None)
            return await self.send_thinking(message.text)
        elif isinstance(message, ToolCallMessage):
            if not message.calls:
                return MessageRef(platform_data=None)
        else:
            return MessageRef(platform_data=None)

        await self._apply_rate_limit()

        text = self._render_message(message)
        chunks = split_text(text, self.max_message_length)
        view = self._build_view(buttons)
        msg = None

        for i, chunk in enumerate(chunks):
            is_last = i == len(chunks) - 1
            try:
                if is_last and view:
                    msg = await self._channel.send(content=chunk, view=view)
                else:
                    msg = await self._channel.send(content=chunk)
                self._last_send = time.time()
            except discord.RateLimited as e:
                _log.warning("Rate limited on send_message: retry_after=%.1fs", e.retry_after)
                self._log_error("send_message", e)
            except Exception as e:
                self._log_error("send_message", e)

            # Throttle between chunks to prevent burst
            if not is_last:
                await asyncio.sleep(MIN_SEND_INTERVAL)

        return MessageRef(platform_data=msg)

    async def edit_message(
        self,
        ref: MessageRef,
        message: PlatformMessage,
        *,
        buttons: Optional[list[ButtonRow]] = None,
    ) -> None:
        """Edit an existing message."""
        msg: Optional[discord.Message] = ref.platform_data
        if not msg:
            return

        view = self._build_view(buttons)

        try:
            await self._apply_rate_limit()
            if isinstance(message, ThinkingMessage):
                embed = self._build_thinking_embed(message.text)
                await msg.edit(embed=embed, content=None, view=view)
            else:
                content = self._render_message(message)
                await msg.edit(content=content, view=view)
            self._last_send = time.time()
        except discord.RateLimited as e:
            _log.warning("Rate limited on edit_message: retry_after=%.1fs", e.retry_after)
            self._log_error("edit_message", e)
        except discord.NotFound:
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
        except discord.RateLimited as e:
            _log.warning("Rate limited on delete_message: retry_after=%.1fs", e.retry_after)
            self._log_error("delete_message", e)
        except discord.NotFound:
            pass
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
        except discord.RateLimited as e:
            _log.warning("Rate limited on send_typing: retry_after=%.1fs", e.retry_after)
        except Exception as e:
            self._log_error("send_typing", e)

    async def send_photo(
        self,
        image: BytesIO,
        caption: Optional[str] = None,
    ) -> MessageRef:
        """Send a single image."""
        try:
            await self._apply_rate_limit()
            image.seek(0)
            file = discord.File(image, filename="image.png")
            msg = await self._channel.send(content=caption, file=file)
            self._last_send = time.time()
            return MessageRef(platform_data=msg)
        except discord.RateLimited as e:
            _log.warning("Rate limited on send_photo: retry_after=%.1fs", e.retry_after)
            self._log_error("send_photo", e)
            return MessageRef(platform_data=None)
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
            await self._apply_rate_limit()
            files = []
            for i, (img_buffer, caption) in enumerate(images):
                img_buffer.seek(0)
                files.append(discord.File(img_buffer, filename=f"image_{i}.png"))

            content = images[0][1] if images else None
            await self._channel.send(content=content, files=files)
            self._last_send = time.time()
        except discord.RateLimited as e:
            _log.warning("Rate limited on send_photos: retry_after=%.1fs", e.retry_after)
            self._log_error("send_photos", e)
        except Exception as e:
            self._log_error("send_photos", e)

    async def send_document(
        self,
        path: str,
        caption: Optional[str] = None,
    ) -> MessageRef:
        """Send a file/document."""
        try:
            await self._apply_rate_limit()
            file = discord.File(path)
            msg = await self._channel.send(content=caption, file=file)
            self._last_send = time.time()
            return MessageRef(platform_data=msg)
        except discord.RateLimited as e:
            _log.warning("Rate limited on send_document: retry_after=%.1fs", e.retry_after)
            self._log_error("send_document", e)
            return MessageRef(platform_data=None)
        except Exception as e:
            self._log_error("send_document", e)
            return MessageRef(platform_data=None)

    async def send_thinking(self, text: str) -> MessageRef:
        """Send thinking content as an embed with grey sidebar."""
        if not text.strip():
            return MessageRef(platform_data=None)

        await self._apply_rate_limit()

        embed = self._build_thinking_embed(text)

        try:
            msg = await self._channel.send(embed=embed)
            self._last_send = time.time()
            return MessageRef(platform_data=msg)
        except discord.RateLimited as e:
            _log.warning("Rate limited on send_thinking: retry_after=%.1fs", e.retry_after)
            self._log_error("send_thinking", e)
            return MessageRef(platform_data=None)
        except Exception as e:
            self._log_error("send_thinking", e)
            return MessageRef(platform_data=None)
