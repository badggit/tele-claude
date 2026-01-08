"""Telegram platform implementation."""

from .client import TelegramClient
from .formatter import (
    TelegramFormatter,
    escape_html,
    format_tool_call,
    format_tool_calls_batch,
    format_tool_output,
    markdown_to_html,
    split_text,
    strip_html_tags,
)

__all__ = [
    "TelegramClient",
    "TelegramFormatter",
    "escape_html",
    "format_tool_call",
    "format_tool_calls_batch",
    "format_tool_output",
    "markdown_to_html",
    "split_text",
    "strip_html_tags",
]
