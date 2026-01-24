"""Telegram platform client implementation.

Wraps python-telegram-bot's Bot class to implement PlatformClient protocol.
Handles rate limiting, retries, and Telegram-specific error handling.
"""

import asyncio
import re
import time
from io import BytesIO
from typing import Optional, Protocol, runtime_checkable

from telegram import Bot, InputMediaPhoto, Message
from telegram.constants import ChatAction

from ..protocol import ButtonRow, ButtonSpec, MessageRef, PlatformClient
from .formatter import TelegramFormatter, markdown_to_html, split_text, strip_html_tags


@runtime_checkable
class ErrorLogger(Protocol):
    """Protocol for error logging capability."""

    def log_error(self, context: str, error: Exception) -> None:
        """Log an error with context."""
        ...

# Rate limiting constants
MIN_SEND_INTERVAL = 1.0  # Minimum seconds between messages
TYPING_ACTION_INTERVAL = 4.0  # Seconds between typing indicators


class TelegramClient(PlatformClient):
    """Telegram implementation of PlatformClient protocol.

    Handles:
    - Message sending/editing with rate limiting
    - Flood control backoff
    - HTML formatting fallbacks
    - Typing indicators
    - Photo and document sending
    """

    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        thread_id: int,
        logger: Optional[ErrorLogger] = None,
    ):
        """Initialize Telegram client.

        Args:
            bot: Telegram Bot instance
            chat_id: Chat ID to send messages to
            thread_id: Forum topic thread ID (0 for non-forum chats)
            logger: Optional SessionLogger for error logging
        """
        self._bot = bot
        self._chat_id = chat_id
        self._thread_id = thread_id
        self._logger = logger
        self._formatter = TelegramFormatter()

        # Rate limiting state
        self._last_send = 0.0
        self._send_interval = MIN_SEND_INTERVAL
        self._last_typing_action = 0.0

    @property
    def max_message_length(self) -> int:
        return 4000

    @property
    def bot(self) -> Bot:
        """Access underlying Telegram Bot (for backwards compatibility)."""
        return self._bot

    @property
    def chat_id(self) -> int:
        return self._chat_id

    @property
    def thread_id(self) -> int:
        return self._thread_id

    def _build_keyboard(self, buttons: Optional[list[ButtonRow]]):
        """Convert ButtonRows to Telegram InlineKeyboardMarkup."""
        if not buttons:
            return None

        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        return InlineKeyboardMarkup([
            [InlineKeyboardButton(b.text, callback_data=b.callback_id)
             for b in row.buttons]
            for row in buttons
        ])

    async def _apply_rate_limit(self) -> None:
        """Wait if necessary to respect rate limits."""
        now = time.time()
        elapsed = now - self._last_send
        if elapsed < self._send_interval:
            await asyncio.sleep(self._send_interval - elapsed)

    def _update_rate_limit(self, success: bool, error: Optional[Exception] = None) -> None:
        """Update rate limiting state based on send result."""
        if success:
            self._send_interval = MIN_SEND_INTERVAL
            self._last_send = time.time()
        elif error and "flood control" in str(error).lower():
            self._send_interval = min(self._send_interval * 2, 30.0)

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
        """Send a new message with rate limiting and chunking."""
        if not text.strip():
            return MessageRef(platform_data=None)

        await self._apply_rate_limit()

        # Convert markdown to Telegram HTML
        html_text = markdown_to_html(text)
        chunks = split_text(html_text, self.max_message_length)

        keyboard = self._build_keyboard(buttons)
        msg = None

        for i, chunk in enumerate(chunks):
            # Only add keyboard to last chunk
            chunk_keyboard = keyboard if i == len(chunks) - 1 else None
            try:
                msg = await self._bot.send_message(
                    chat_id=self._chat_id,
                    message_thread_id=self._thread_id,
                    text=chunk,
                    parse_mode="HTML",
                    reply_markup=chunk_keyboard,
                )
                self._update_rate_limit(success=True)
            except Exception as e:
                self._update_rate_limit(success=False, error=e)
                # Try plain text fallback
                if "parse entities" in str(e).lower() or "can't parse" in str(e).lower():
                    try:
                        plain_text = strip_html_tags(chunk)
                        msg = await self._bot.send_message(
                            chat_id=self._chat_id,
                            message_thread_id=self._thread_id,
                            text=plain_text,
                            reply_markup=chunk_keyboard,
                        )
                        self._update_rate_limit(success=True)
                    except Exception as plain_err:
                        self._log_error("send_message_plain", plain_err)
                else:
                    self._log_error("send_message", e)

        return MessageRef(platform_data=msg)

    async def edit_message(
        self,
        ref: MessageRef,
        text: str,
        *,
        buttons: Optional[list[ButtonRow]] = None,
    ) -> None:
        """Edit an existing message.

        Note: Caller (session.py send_or_edit_response) handles overflow/truncation.
        Text should already be within max_message_length when this is called.
        """
        msg: Optional[Message] = ref.platform_data
        if not msg:
            return

        html_text = markdown_to_html(text)
        keyboard = self._build_keyboard(buttons)

        try:
            await self._bot.edit_message_text(
                chat_id=self._chat_id,
                message_id=msg.message_id,
                text=html_text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        except Exception as e:
            error_str = str(e).lower()

            # Not an error - message content unchanged
            if "message is not modified" in error_str:
                return

            # Try plain text fallback
            if "parse entities" in error_str or "can't parse" in error_str:
                try:
                    plain_text = strip_html_tags(html_text)
                    await self._bot.edit_message_text(
                        chat_id=self._chat_id,
                        message_id=msg.message_id,
                        text=plain_text,
                        reply_markup=keyboard,
                    )
                except Exception as plain_err:
                    self._log_error("edit_message_plain", plain_err)
            else:
                self._log_error("edit_message", e)

    async def delete_message(self, ref: MessageRef) -> None:
        """Delete a message."""
        msg: Optional[Message] = ref.platform_data
        if not msg:
            return

        try:
            await self._bot.delete_message(
                chat_id=self._chat_id,
                message_id=msg.message_id,
            )
        except Exception as e:
            self._log_error("delete_message", e)

    async def send_typing(self) -> None:
        """Send typing indicator if enough time has passed."""
        now = time.time()
        if now - self._last_typing_action < TYPING_ACTION_INTERVAL:
            return

        try:
            await self._bot.send_chat_action(
                chat_id=self._chat_id,
                message_thread_id=self._thread_id,
                action=ChatAction.TYPING,
            )
            self._last_typing_action = now
        except Exception as e:
            # Don't log flood control errors for typing
            if "flood" not in str(e).lower():
                self._log_error("send_typing", e)

    async def send_photo(
        self,
        image: BytesIO,
        caption: Optional[str] = None,
    ) -> MessageRef:
        """Send a single photo."""
        try:
            msg = await self._bot.send_photo(
                chat_id=self._chat_id,
                message_thread_id=self._thread_id,
                photo=image,
                caption=caption,
            )
            return MessageRef(platform_data=msg)
        except Exception as e:
            self._log_error("send_photo", e)
            return MessageRef(platform_data=None)

    async def send_photos(
        self,
        images: list[tuple[BytesIO, str]],
    ) -> None:
        """Send multiple images as a media group."""
        if not images:
            return

        try:
            if len(images) == 1:
                img_buffer, caption = images[0]
                await self._bot.send_photo(
                    chat_id=self._chat_id,
                    message_thread_id=self._thread_id,
                    photo=img_buffer,
                    caption=caption,
                )
            else:
                media = [
                    InputMediaPhoto(media=img_buffer, caption=caption)
                    for img_buffer, caption in images
                ]
                await self._bot.send_media_group(
                    chat_id=self._chat_id,
                    message_thread_id=self._thread_id,
                    media=media,
                )
        except Exception as e:
            self._log_error("send_photos", e)

    async def send_document(
        self,
        path: str,
        caption: Optional[str] = None,
    ) -> MessageRef:
        """Send a file/document."""
        try:
            with open(path, 'rb') as f:
                msg = await self._bot.send_document(
                    chat_id=self._chat_id,
                    message_thread_id=self._thread_id,
                    document=f,
                    caption=caption,
                )
            return MessageRef(platform_data=msg)
        except Exception as e:
            self._log_error("send_document", e)
            return MessageRef(platform_data=None)

    async def send_thinking(self, text: str) -> MessageRef:
        """Send thinking content as italic text with brain emoji.

        Bypasses markdown_to_html to directly construct HTML,
        avoiding double-escaping issues.
        """
        if not text.strip():
            return MessageRef(platform_data=None)

        await self._apply_rate_limit()

        # Escape HTML entities in the raw text
        from .formatter import escape_html
        safe_text = escape_html(text)

        # Truncate if needed
        max_len = self.max_message_length - 20  # Room for emoji and tags
        if len(safe_text) > max_len:
            safe_text = safe_text[:max_len - 3] + "..."

        # Construct HTML directly (no markdown processing)
        html_text = f"🧠 <i>{safe_text}</i>"

        try:
            msg = await self._bot.send_message(
                chat_id=self._chat_id,
                message_thread_id=self._thread_id,
                text=html_text,
                parse_mode="HTML",
            )
            self._update_rate_limit(success=True)
            return MessageRef(platform_data=msg)
        except Exception as e:
            self._update_rate_limit(success=False, error=e)
            # Fallback to plain text
            if "parse entities" in str(e).lower() or "can't parse" in str(e).lower():
                try:
                    plain_text = f"🧠 {text}"
                    if len(plain_text) > self.max_message_length:
                        plain_text = plain_text[:self.max_message_length - 3] + "..."
                    msg = await self._bot.send_message(
                        chat_id=self._chat_id,
                        message_thread_id=self._thread_id,
                        text=plain_text,
                    )
                    self._update_rate_limit(success=True)
                    return MessageRef(platform_data=msg)
                except Exception as plain_err:
                    self._log_error("send_thinking_plain", plain_err)
            else:
                self._log_error("send_thinking", e)
            return MessageRef(platform_data=None)


async def send_with_fallback(
    client: TelegramClient,
    text: str,
    existing_ref: Optional[MessageRef] = None,
    max_retries: int = 3,
) -> Optional[MessageRef]:
    """Send or edit a message with retry logic.

    This is a helper for complex streaming scenarios where we need
    retry logic with exponential backoff.
    """
    html_text = markdown_to_html(text)

    for attempt in range(max_retries):
        try:
            if existing_ref and existing_ref.platform_data:
                await client.edit_message(existing_ref, text)
                return existing_ref
            else:
                return await client.send_message(text)
        except Exception as e:
            error_str = str(e).lower()

            if "message is not modified" in error_str:
                return existing_ref

            # Retry with backoff for transient errors
            if "flood control" in error_str or "retry" in error_str or "timed out" in error_str:
                wait_time = 2 ** attempt
                match = re.search(r'retry in (\d+)', error_str)
                if match:
                    wait_time = min(int(match.group(1)), 30)

                if attempt < max_retries - 1:
                    await asyncio.sleep(wait_time)
                    continue

            break

    return None
