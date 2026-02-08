from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any, Protocol, runtime_checkable

from platforms.protocol import ButtonRow, MessageRef, PlatformMessage


def make_session_key(platform: str, **ids: int | None) -> str:
    """Generate collision-free session key."""
    if platform == "telegram":
        chat_id = ids["chat_id"]
        thread_id = ids.get("thread_id")
        if thread_id:
            return f"telegram:{chat_id}:{thread_id}"
        return f"telegram:{chat_id}"

    if platform == "discord":
        channel_id = ids["channel_id"]
        return f"discord:{channel_id}"

    raise ValueError(f"Unknown platform: {platform}")


@dataclass
class Trigger:
    """Incoming event from any transport."""

    platform: str
    session_key: str
    prompt: str
    images: list[str] = field(default_factory=list)
    reply_context: dict[str, Any] = field(default_factory=dict)
    source: str = "user"


@dataclass
class SessionStats:
    """Runtime statistics for a session."""

    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    message_count: int = 0
    turn_count: int = 0
    interrupt_count: int = 0
    error_count: int = 0


@dataclass
class ReplyCapabilities:
    """What this reply target supports."""

    can_edit: bool = True
    can_buttons: bool = True
    can_typing: bool = True
    max_length: int = 4000
    max_buttons_per_row: int = 3


@runtime_checkable
class ReplyTarget(Protocol):
    """Interface for sending replies back to the originating platform."""

    @property
    def capabilities(self) -> ReplyCapabilities:
        """What operations this target supports."""
        ...

    async def send(self, message: PlatformMessage) -> MessageRef:
        """Send a new message."""
        ...

    async def edit(self, ref: MessageRef, message: PlatformMessage) -> None:
        """Edit an existing message."""
        ...

    async def send_buttons(
        self,
        message: PlatformMessage,
        buttons: list[ButtonRow],
    ) -> MessageRef:
        """Send a new message with buttons."""
        ...

    async def typing(self) -> None:
        """Show typing indicator."""
        ...
