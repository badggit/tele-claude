"""
Generate diff images for file edits using Pygments and Pillow.
"""
import difflib
from io import BytesIO
from typing import Optional

from pygments import highlight
from pygments.lexers import DiffLexer
from pygments.formatters import ImageFormatter
from pygments.styles import get_style_by_name


# Default style - dark theme works well in Telegram (monokai)
DEFAULT_STYLE = "monokai"
DEFAULT_FONT_SIZE = 14
DEFAULT_LINE_PAD = 2
MAX_DIFF_LINES = 50  # Limit to avoid huge images


def generate_diff(old_text: str, new_text: str, filename: str = "file") -> str:
    """Generate unified diff between old and new text."""
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)

    # Ensure lines end with newline for proper diff
    if old_lines and not old_lines[-1].endswith('\n'):
        old_lines[-1] += '\n'
    if new_lines and not new_lines[-1].endswith('\n'):
        new_lines[-1] += '\n'

    diff = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=f"a/{filename}",
        tofile=f"b/{filename}",
        lineterm=""
    )

    return "".join(diff)


def truncate_diff(diff_text: str, max_lines: int = MAX_DIFF_LINES) -> tuple[str, bool]:
    """Truncate diff if too long. Returns (diff, was_truncated)."""
    lines = diff_text.splitlines(keepends=True)
    if len(lines) <= max_lines:
        return diff_text, False

    truncated = "".join(lines[:max_lines])
    truncated += f"\n... ({len(lines) - max_lines} more lines)\n"
    return truncated, True


def diff_to_image(
    old_text: str,
    new_text: str,
    filename: str = "file",
    style: str = DEFAULT_STYLE,
    font_size: int = DEFAULT_FONT_SIZE,
    max_lines: int = MAX_DIFF_LINES
) -> Optional[BytesIO]:
    """
    Generate an image from a diff between old and new text.

    Returns a BytesIO buffer containing PNG image data, or None if no diff.
    """
    # Generate diff
    diff_text = generate_diff(old_text, new_text, filename)

    if not diff_text.strip():
        return None

    # Truncate if needed
    diff_text, _ = truncate_diff(diff_text, max_lines)

    # Create image using Pygments
    formatter = ImageFormatter(
        style=style,
        font_size=font_size,
        line_pad=DEFAULT_LINE_PAD,
        line_numbers=True,
        image_format="png"
    )

    # Highlight with diff lexer
    highlighted = highlight(diff_text, DiffLexer(), formatter)

    # Return as BytesIO buffer
    buffer = BytesIO(highlighted)
    buffer.seek(0)
    return buffer


def edit_to_image(
    file_path: str,
    old_string: str,
    new_string: str,
    style: str = DEFAULT_STYLE,
    max_lines: int = MAX_DIFF_LINES
) -> Optional[BytesIO]:
    """
    Generate diff image from Edit tool parameters.

    Args:
        file_path: Path to the file being edited
        old_string: The original text being replaced
        new_string: The new text replacing old_string
        style: Pygments style name
        max_lines: Maximum lines before truncation

    Returns:
        BytesIO buffer with PNG image, or None if no changes
    """
    # Extract just the filename for display
    filename = file_path.split("/")[-1] if "/" in file_path else file_path

    return diff_to_image(
        old_text=old_string,
        new_text=new_string,
        filename=filename,
        style=style,
        max_lines=max_lines
    )
