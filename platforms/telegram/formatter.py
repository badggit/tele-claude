"""Telegram-specific message formatting.

Converts Claude's markdown output to Telegram's HTML subset.
Telegram only supports: <b>, <i>, <u>, <s>, <code>, <pre>, <a href="">
"""

import re
from typing import Any, Optional

import mistune

from ..protocol import MessageFormatter


def escape_html(text: str) -> str:
    """Escape HTML entities for Telegram."""
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def strip_html_tags(text: str) -> str:
    """Remove all HTML tags from text, leaving only content."""
    return re.sub(r'<[^>]+>', '', text)


class TelegramHTMLRenderer(mistune.HTMLRenderer):
    """Custom mistune renderer that outputs Telegram-compatible HTML.

    Telegram only supports a subset of HTML tags:
    <b>, <strong>, <i>, <em>, <u>, <ins>, <s>, <strike>, <del>,
    <span class="tg-spoiler">, <a href="">, <code>, <pre>
    """

    def text(self, text: str) -> str:
        """Escape HTML entities in plain text."""
        return escape_html(text)

    def emphasis(self, text: str) -> str:
        """Render *italic* text."""
        return f'<i>{text}</i>'

    def strong(self, text: str) -> str:
        """Render **bold** text."""
        return f'<b>{text}</b>'

    def codespan(self, text: str) -> str:
        """Render `inline code`."""
        return f'<code>{escape_html(text)}</code>'

    def block_code(self, code: str, info: Optional[str] = None) -> str:
        """Render ```code blocks```."""
        return f'<pre>{escape_html(code)}</pre>\n'

    def link(self, text: str, url: str, title: Optional[str] = None) -> str:
        """Render [text](url) links."""
        return f'<a href="{escape_html(url)}">{text}</a>'

    def strikethrough(self, text: str) -> str:
        """Render ~~strikethrough~~ text."""
        return f'<s>{text}</s>'

    def heading(self, text: str, level: int, **attrs: Any) -> str:
        """Render headings as bold text (Telegram doesn't support h1-h6)."""
        prefix = '#' * level
        return f'<b>{prefix} {text}</b>\n'

    def paragraph(self, text: str) -> str:
        """Render paragraphs with newlines."""
        return f'{text}\n\n'

    def linebreak(self) -> str:
        """Render line breaks."""
        return '\n'

    def softbreak(self) -> str:
        """Render soft breaks (single newlines in source)."""
        return '\n'

    def blank_line(self) -> str:
        """Render blank lines."""
        return '\n'

    def thematic_break(self) -> str:
        """Render horizontal rules as dashes."""
        return '\n---\n'

    def block_quote(self, text: str) -> str:
        """Render blockquotes with > prefix."""
        lines = text.strip().split('\n')
        quoted = '\n'.join(f'> {line}' for line in lines)
        return f'{quoted}\n\n'

    def list(self, text: str, ordered: bool, **attrs: Any) -> str:
        """Render lists."""
        return f'{text}\n'

    def list_item(self, text: str) -> str:
        """Render list items."""
        return f'• {text.strip()}\n'

    def image(self, text: str, url: str, title: Optional[str] = None) -> str:
        """Render images as links (Telegram doesn't support inline images)."""
        return f'[{text}]({escape_html(url)})'

    def table(self, text: str) -> str:
        """Render table as plain text."""
        return f'{text}\n'

    def table_head(self, text: str) -> str:
        """Render table header."""
        return f'{text}'

    def table_body(self, text: str) -> str:
        """Render table body."""
        return text

    def table_row(self, text: str) -> str:
        """Render table row as pipe-separated values."""
        return f'{text}|\n'

    def table_cell(self, text: str, align: Optional[str] = None, head: bool = False) -> str:
        """Render table cell."""
        if head:
            return f'| <b>{text}</b> '
        return f'| {text} '


# Global markdown parser instance
_telegram_md = mistune.create_markdown(
    renderer=TelegramHTMLRenderer(),
    plugins=['strikethrough', 'table']
)


def markdown_to_html(text: str) -> str:
    """Convert markdown to Telegram-compatible HTML using mistune."""
    try:
        result = _telegram_md(text)
        if isinstance(result, tuple):
            result = result[0]
        result_str: str = str(result) if not isinstance(result, str) else result
        result_str = re.sub(r'\n{3,}', '\n\n', result_str)
        return result_str.strip()
    except Exception:
        return escape_html(text)


def split_text(text: str, max_len: int = 4000) -> list[str]:
    """Split text into chunks suitable for Telegram messages."""
    if len(text) <= max_len:
        return [text]

    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break

        split_pos = text.rfind('\n', 0, max_len)
        if split_pos == -1 or split_pos < max_len // 2:
            split_pos = text.rfind(' ', 0, max_len)
        if split_pos == -1 or split_pos < max_len // 2:
            split_pos = max_len

        chunks.append(text[:split_pos])
        text = text[split_pos:].lstrip()

    return chunks


class TelegramFormatter(MessageFormatter):
    """Telegram implementation of MessageFormatter protocol."""

    def format_markdown(self, text: str) -> str:
        """Convert Claude's markdown to Telegram HTML."""
        return markdown_to_html(text)

    def escape_text(self, text: str) -> str:
        """Escape HTML entities."""
        return escape_html(text)

    def bold(self, text: str) -> str:
        return f'<b>{text}</b>'

    def italic(self, text: str) -> str:
        return f'<i>{text}</i>'

    def code(self, text: str) -> str:
        return f'<code>{escape_html(text)}</code>'

    def code_block(self, text: str, language: Optional[str] = None) -> str:
        return f'<pre>{escape_html(text)}</pre>'

    def link(self, text: str, url: str) -> str:
        return f'<a href="{escape_html(url)}">{text}</a>'

    def blockquote(self, text: str) -> str:
        lines = text.strip().split('\n')
        return '\n'.join(f'> {line}' for line in lines)

    def format_tool_call(self, name: str, args: dict) -> str:
        """Format a tool call for display (HTML)."""
        parts = []
        for k, v in args.items():
            v_str = str(v)
            if len(v_str) > 50:
                v_str = v_str[:50] + "..."
            v_str = self.escape_text(v_str)
            parts.append(f"{k}={v_str}")

        args_str = ", ".join(parts) if parts else ""
        return f"🔧 {self.bold(name)}({args_str})"

    def format_tool_calls_batch(self, tool_name: str, calls: list[tuple[str, dict]]) -> str:
        """Format multiple tool calls of same type (HTML)."""
        items = []
        for name, input_dict in calls:
            key_arg = None
            for key in ['file_path', 'path', 'pattern', 'command', 'query', 'prompt', 'url']:
                if key in input_dict:
                    key_arg = str(input_dict[key])
                    break

            if key_arg is None and input_dict:
                key_arg = str(list(input_dict.values())[0])

            if key_arg:
                if len(key_arg) > 60:
                    key_arg = key_arg[:57] + "..."
                key_arg = self.escape_text(key_arg)
                items.append(f"• {key_arg}")

        if not items:
            return f"🔧 {self.bold(tool_name)} (×{len(calls)})"

        items_str = "\n".join(items)
        return f"🔧 {self.bold(tool_name)} (×{len(calls)}):\n{items_str}"


def format_tool_call(name: str, input_dict: dict, formatter: Optional[MessageFormatter] = None) -> str:
    """Format a tool call for display.

    Args:
        name: Tool name
        input_dict: Tool arguments
        formatter: Optional formatter (defaults to Telegram HTML)
    """
    if formatter is None:
        formatter = TelegramFormatter()

    parts = []
    for k, v in input_dict.items():
        v_str = str(v)
        if len(v_str) > 50:
            v_str = v_str[:50] + "..."
        v_str = formatter.escape_text(v_str)
        parts.append(f"{k}={v_str}")

    args_str = ", ".join(parts) if parts else ""
    return f"🔧 {formatter.bold(name)}({args_str})"


def format_tool_calls_batch(
    tool_name: str,
    calls: list[tuple[str, dict]],
    formatter: Optional[MessageFormatter] = None
) -> str:
    """Format multiple tool calls of same type as a single message."""
    if formatter is None:
        formatter = TelegramFormatter()

    items = []
    for name, input_dict in calls:
        key_arg = None
        for key in ['file_path', 'path', 'pattern', 'command', 'query', 'prompt', 'url']:
            if key in input_dict:
                key_arg = str(input_dict[key])
                break

        if key_arg is None and input_dict:
            key_arg = str(list(input_dict.values())[0])

        if key_arg:
            if len(key_arg) > 60:
                key_arg = key_arg[:57] + "..."
            key_arg = formatter.escape_text(key_arg)
            items.append(f"  • {key_arg}")
        else:
            items.append("  • (no args)")

    return f"🔧 {formatter.bold(tool_name)} ({len(calls)} calls)\n" + "\n".join(items)


def format_tool_output(content: Any) -> str:
    """Format tool output for display, truncating if needed."""
    if content is None:
        return ""

    text = str(content)
    if len(text) > 1000:
        return text[:1000] + "\n... (truncated)"
    return text
