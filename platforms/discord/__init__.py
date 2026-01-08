"""Discord platform implementation.

Handlers are NOT exported here to avoid circular imports.
Import directly: from platforms.discord.handlers import ...
"""

from .client import DiscordClient
from .formatter import DiscordFormatter

__all__ = [
    "DiscordClient",
    "DiscordFormatter",
]
