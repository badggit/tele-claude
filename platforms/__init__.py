"""Multi-platform abstraction layer for Claude bot."""

from .protocol import (
    ButtonSpec,
    ButtonRow,
    MessageRef,
    PlatformClient,
    MessageFormatter,
)

__all__ = [
    "ButtonSpec",
    "ButtonRow",
    "MessageRef",
    "PlatformClient",
    "MessageFormatter",
]
