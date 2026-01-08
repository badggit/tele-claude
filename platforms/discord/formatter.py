"""Discord message formatter.

Discord uses standard Markdown, so most formatting is passthrough.
"""

from typing import Optional

from ..protocol import MessageFormatter


class DiscordFormatter(MessageFormatter):
    """Discord implementation of MessageFormatter protocol.
    
    Discord natively supports Markdown, so most formatting is passthrough.
    """

    def format_markdown(self, text: str) -> str:
        """Discord uses markdown natively - passthrough."""
        return text

    def escape_text(self, text: str) -> str:
        """Escape Discord markdown special characters."""
        # Discord markdown escapes
        chars_to_escape = ['*', '_', '~', '`', '|', '>', '#', '@', '!']
        result = text
        for char in chars_to_escape:
            result = result.replace(char, f'\\{char}')
        return result

    def bold(self, text: str) -> str:
        return f"**{text}**"

    def italic(self, text: str) -> str:
        return f"*{text}*"

    def code(self, text: str) -> str:
        return f"`{text}`"

    def code_block(self, text: str, language: Optional[str] = None) -> str:
        lang = language or ""
        return f"```{lang}\n{text}\n```"

    def link(self, text: str, url: str) -> str:
        return f"[{text}]({url})"

    def blockquote(self, text: str) -> str:
        lines = text.split('\n')
        return '\n'.join(f"> {line}" for line in lines)

    def format_tool_call(self, name: str, args: dict) -> str:
        """Format a tool call for display (Markdown)."""
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
        """Format multiple tool calls of same type (Markdown)."""
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


def split_text(text: str, max_length: int = 2000) -> list[str]:
    """Split text into chunks that fit Discord's message limit.
    
    Tries to split at natural boundaries (newlines, spaces).
    """
    if len(text) <= max_length:
        return [text]
    
    chunks = []
    current = ""
    
    for line in text.split('\n'):
        if len(current) + len(line) + 1 <= max_length:
            current = current + '\n' + line if current else line
        else:
            if current:
                chunks.append(current)
            # Handle lines longer than max_length
            if len(line) > max_length:
                words = line.split(' ')
                current = ""
                for word in words:
                    if len(current) + len(word) + 1 <= max_length:
                        current = current + ' ' + word if current else word
                    else:
                        if current:
                            chunks.append(current)
                        current = word[:max_length]  # Truncate very long words
            else:
                current = line
    
    if current:
        chunks.append(current)
    
    return chunks
