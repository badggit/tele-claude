"""Protocol definitions for multi-platform bot abstraction.

This module defines the interfaces that platform implementations must satisfy.
Uses Python's Protocol for structural typing (duck typing with type hints).
"""

from dataclasses import dataclass
from io import BytesIO
from typing import Any, Optional, Protocol, runtime_checkable


@dataclass
class ButtonSpec:
    """Platform-agnostic button specification."""
    text: str
    callback_id: str  # Unique ID for routing callbacks


@dataclass
class ButtonRow:
    """A row of buttons in a keyboard layout."""
    buttons: list[ButtonSpec]


@dataclass
class MessageRef:
    """Opaque reference to a sent message (for editing).

    Each platform stores what it needs internally:
    - Telegram: Message object with chat_id, message_id
    - Discord: discord.Message object
    """
    platform_data: Any


@runtime_checkable
class PlatformClient(Protocol):
    """Abstract messaging operations across platforms.

    Implementations handle platform-specific:
    - Rate limiting (Telegram flood control, Discord built-in)
    - Message formatting requirements
    - Error handling and retries
    """

    @property
    def max_message_length(self) -> int:
        """Platform's maximum message length.

        - Telegram: 4096 characters
        - Discord: 2000 characters
        """
        ...

    async def send_message(
        self,
        text: str,
        *,
        buttons: Optional[list[ButtonRow]] = None,
    ) -> MessageRef:
        """Send a new message to the active channel/thread.

        Args:
            text: Message content (already formatted for platform)
            buttons: Optional keyboard buttons

        Returns:
            Reference for later editing
        """
        ...

    async def edit_message(
        self,
        ref: MessageRef,
        text: str,
        *,
        buttons: Optional[list[ButtonRow]] = None,
    ) -> None:
        """Edit an existing message.

        Args:
            ref: Message reference from send_message
            text: New message content
            buttons: Optional updated keyboard
        """
        ...

    async def delete_message(self, ref: MessageRef) -> None:
        """Delete a message.

        Args:
            ref: Message reference from send_message
        """
        ...

    async def send_typing(self) -> None:
        """Show typing indicator.

        Platform behavior:
        - Telegram: Lasts ~5 seconds, needs refresh
        - Discord: Lasts ~10 seconds via context manager
        """
        ...

    async def send_photo(
        self,
        image: BytesIO,
        caption: Optional[str] = None,
    ) -> MessageRef:
        """Send a single image.

        Args:
            image: Image data as BytesIO buffer
            caption: Optional caption text

        Returns:
            Reference for the sent message
        """
        ...

    async def send_photos(
        self,
        images: list[tuple[BytesIO, str]],
    ) -> None:
        """Send multiple images as album/gallery.

        Args:
            images: List of (image_buffer, caption) tuples
        """
        ...

    async def send_document(
        self,
        path: str,
        caption: Optional[str] = None,
    ) -> MessageRef:
        """Send a file/document.

        Args:
            path: Path to the file
            caption: Optional caption text

        Returns:
            Reference for the sent message
        """
        ...


@runtime_checkable
class MessageFormatter(Protocol):
    """Abstract text formatting for different platforms.

    Transforms Claude's markdown output to platform-specific format:
    - Telegram: HTML subset (<b>, <i>, <code>, <pre>)
    - Discord: Native markdown (**, *, `, ```)
    """

    def format_markdown(self, text: str) -> str:
        """Convert Claude's markdown to platform format.

        Args:
            text: Raw markdown text from Claude

        Returns:
            Platform-formatted text
        """
        ...

    def escape_text(self, text: str) -> str:
        """Escape special characters for the platform.

        Args:
            text: Raw text that may contain special chars

        Returns:
            Safely escaped text
        """
        ...

    def bold(self, text: str) -> str:
        """Format text as bold."""
        ...

    def italic(self, text: str) -> str:
        """Format text as italic."""
        ...

    def code(self, text: str) -> str:
        """Format text as inline code."""
        ...

    def code_block(self, text: str, language: Optional[str] = None) -> str:
        """Format text as a code block.

        Args:
            text: Code content
            language: Optional language for syntax highlighting
        """
        ...

    def link(self, text: str, url: str) -> str:
        """Format a hyperlink.

        Args:
            text: Link display text
            url: Link URL
        """
        ...

    def blockquote(self, text: str) -> str:
        """Format text as a blockquote."""
        ...

    def format_tool_call(self, name: str, args: dict) -> str:
        """Format a tool call for display.

        Args:
            name: Tool name
            args: Tool arguments dict

        Returns:
            Formatted tool call string (e.g., "🔧 **Read**(file=foo.py)")
        """
        ...

    def format_tool_calls_batch(self, tool_name: str, calls: list[tuple[str, dict]]) -> str:
        """Format multiple tool calls of same type as a single message.

        Args:
            tool_name: Common name for the batch
            calls: List of (name, args) tuples

        Returns:
            Formatted batch message
        """
        ...
