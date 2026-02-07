"""Multi-platform abstraction layer for Claude bot."""

from .protocol import (
    ButtonSpec,
    ButtonRow,
    MessageRef,
    PlatformClient,
    MessageFormatter,
    TextMessage,
    ToolCallMessage,
    ThinkingMessage,
    PlatformMessage,
)

__all__ = [
    "ButtonSpec",
    "ButtonRow",
    "MessageRef",
    "PlatformClient",
    "MessageFormatter",
    "TextMessage",
    "ToolCallMessage",
    "ThinkingMessage",
    "PlatformMessage",
]
