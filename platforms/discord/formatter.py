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
