"""
Side-by-side diff image renderer using Pygments + Pillow.
Pure Python, no external dependencies.
"""
import difflib
from io import BytesIO
from typing import Optional, Any
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont
from pygments import lex
from pygments.lexers import get_lexer_for_filename, TextLexer
from pygments.styles import get_style_by_name


# Configuration
MAX_LINES = 40
FONT_SIZE = 13
LINE_HEIGHT = 18
CHAR_WIDTH = 8  # Approximate for monospace
PADDING = 10
GUTTER_WIDTH = 40
MIN_SIDE_WIDTH = 350

# Colors (GitHub dark theme inspired)
BG_COLOR = (13, 17, 23)  # #0d1117
FG_COLOR = (201, 209, 217)  # #c9d1d9
GUTTER_BG = (22, 27, 34)  # #161b22
GUTTER_FG = (110, 118, 129)  # #6e7681
# Pre-blended colors for better visibility
ADD_BG = (28, 56, 37)  # dark green background
ADD_BG_STRONG = (36, 75, 47)  # stronger green for pure inserts
DEL_BG = (62, 32, 34)  # dark red background
DEL_BG_STRONG = (82, 38, 40)  # stronger red for pure deletes
BORDER_COLOR = (48, 54, 61)  # #30363d
HEADER_BG = (22, 27, 34)  # #161b22


@dataclass
class DiffLine:
    line_no_left: Optional[int]
    line_no_right: Optional[int]
    left_text: str
    right_text: str
    change_type: str  # 'equal', 'insert', 'delete', 'replace'


def _compute_side_by_side_diff(old_text: str, new_text: str) -> list[DiffLine]:
    """Compute side-by-side diff lines."""
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()

    matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
    result: list[DiffLine] = []

    left_no = 1
    right_no = 1

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            for i in range(i2 - i1):
                result.append(DiffLine(
                    line_no_left=left_no,
                    line_no_right=right_no,
                    left_text=old_lines[i1 + i],
                    right_text=new_lines[j1 + i],
                    change_type='equal'
                ))
                left_no += 1
                right_no += 1
        elif tag == 'replace':
            max_len = max(i2 - i1, j2 - j1)
            for i in range(max_len):
                left_idx = i1 + i if i < (i2 - i1) else None
                right_idx = j1 + i if i < (j2 - j1) else None
                result.append(DiffLine(
                    line_no_left=left_no if left_idx is not None else None,
                    line_no_right=right_no if right_idx is not None else None,
                    left_text=old_lines[left_idx] if left_idx is not None else '',
                    right_text=new_lines[right_idx] if right_idx is not None else '',
                    change_type='replace'
                ))
                if left_idx is not None:
                    left_no += 1
                if right_idx is not None:
                    right_no += 1
        elif tag == 'delete':
            for i in range(i2 - i1):
                result.append(DiffLine(
                    line_no_left=left_no,
                    line_no_right=None,
                    left_text=old_lines[i1 + i],
                    right_text='',
                    change_type='delete'
                ))
                left_no += 1
        elif tag == 'insert':
            for i in range(j2 - j1):
                result.append(DiffLine(
                    line_no_left=None,
                    line_no_right=right_no,
                    left_text='',
                    right_text=new_lines[j1 + i],
                    change_type='insert'
                ))
                right_no += 1

    return result


def _get_lexer(filename: str) -> Any:
    """Get Pygments lexer for filename."""
    try:
        return get_lexer_for_filename(filename)
    except Exception:
        return TextLexer()


def _get_token_colors(style_name: str = 'monokai') -> dict[Any, tuple[int, int, int]]:
    """Get color mapping from Pygments style."""
    style = get_style_by_name(style_name)
    colors: dict[Any, tuple[int, int, int]] = {}
    for token, style_def in style:
        color = style_def.get('color')
        if color:
            colors[token] = (int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16))
        else:
            colors[token] = FG_COLOR
    return colors


def _tokenize_line(text: str, lexer: Any) -> list[tuple[Any, str]]:
    """Tokenize a single line."""
    if not text:
        return []
    tokens = list(lex(text, lexer))
    result = []
    for ttype, value in tokens:
        value = value.replace('\n', '')
        if value:
            result.append((ttype, value))
    return result


def _try_load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Try to load a monospace font."""
    font_paths = [
        "/System/Library/Fonts/SFNSMono.ttf",
        "/System/Library/Fonts/Monaco.ttf",
        "/System/Library/Fonts/Menlo.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
    ]
    for path in font_paths:
        try:
            return ImageFont.truetype(path, size)
        except (IOError, OSError):
            continue
    return ImageFont.load_default()


def edit_to_image(
    file_path: str,
    old_string: str,
    new_string: str,
    max_lines: int = MAX_LINES
) -> Optional[BytesIO]:
    """
    Generate side-by-side diff image.

    Args:
        file_path: Path to the file being edited (used for syntax highlighting)
        old_string: The original text being replaced
        new_string: The new text replacing old_string
        max_lines: Maximum lines before truncation

    Returns:
        BytesIO buffer with PNG image, or None if no changes
    """
    diff_lines = _compute_side_by_side_diff(old_string, new_string)

    if not diff_lines:
        return None

    # Truncate if needed
    truncated = len(diff_lines) > max_lines
    if truncated:
        diff_lines = diff_lines[:max_lines]

    # Get lexer and colors
    lexer = _get_lexer(file_path)
    token_colors = _get_token_colors('monokai')

    # Calculate dimensions
    max_left_len = max((len(d.left_text) for d in diff_lines), default=0)
    max_right_len = max((len(d.right_text) for d in diff_lines), default=0)

    side_width = max(MIN_SIDE_WIDTH, min(max_left_len, max_right_len) * CHAR_WIDTH + GUTTER_WIDTH + PADDING * 2)
    total_width = side_width * 2 + 3  # 3px divider

    header_height = 28
    content_height = len(diff_lines) * LINE_HEIGHT + PADDING * 2
    if truncated:
        content_height += 24
    total_height = header_height + content_height

    # Create image
    img = Image.new('RGB', (total_width, total_height), BG_COLOR)
    draw = ImageDraw.Draw(img, 'RGBA')
    font = _try_load_font(FONT_SIZE)

    # Draw header
    filename = file_path.split("/")[-1] if "/" in file_path else file_path
    draw.rectangle([0, 0, total_width, header_height], fill=HEADER_BG)
    draw.text((PADDING, 6), filename, fill=FG_COLOR, font=font)

    # Draw divider line
    divider_x = side_width
    draw.line([(divider_x, header_height), (divider_x, total_height)], fill=BORDER_COLOR, width=1)

    # Draw diff lines
    y = header_height + PADDING

    for diff_line in diff_lines:
        # Left side
        left_x = 0

        # Line number gutter (left)
        draw.rectangle([left_x, y, left_x + GUTTER_WIDTH, y + LINE_HEIGHT], fill=GUTTER_BG)
        if diff_line.line_no_left is not None:
            ln_text = str(diff_line.line_no_left)
            draw.text((left_x + GUTTER_WIDTH - len(ln_text) * CHAR_WIDTH - 4, y + 2),
                      ln_text, fill=GUTTER_FG, font=font)

        # Left content background
        if diff_line.change_type == 'delete':
            draw.rectangle([left_x + GUTTER_WIDTH, y, divider_x, y + LINE_HEIGHT], fill=DEL_BG_STRONG)
        elif diff_line.change_type == 'replace' and diff_line.left_text:
            draw.rectangle([left_x + GUTTER_WIDTH, y, divider_x, y + LINE_HEIGHT], fill=DEL_BG)

        # Left text with syntax highlighting
        text_x = left_x + GUTTER_WIDTH + 6
        if diff_line.left_text:
            tokens = _tokenize_line(diff_line.left_text, lexer)
            for ttype, value in tokens:
                color = token_colors.get(ttype, FG_COLOR)
                while color == FG_COLOR and ttype.parent:
                    ttype = ttype.parent
                    color = token_colors.get(ttype, FG_COLOR)
                draw.text((text_x, y + 2), value, fill=color, font=font)
                text_x += len(value) * CHAR_WIDTH

        # Right side
        right_x = divider_x + 3

        # Line number gutter (right)
        draw.rectangle([right_x, y, right_x + GUTTER_WIDTH, y + LINE_HEIGHT], fill=GUTTER_BG)
        if diff_line.line_no_right is not None:
            ln_text = str(diff_line.line_no_right)
            draw.text((right_x + GUTTER_WIDTH - len(ln_text) * CHAR_WIDTH - 4, y + 2),
                      ln_text, fill=GUTTER_FG, font=font)

        # Right content background
        if diff_line.change_type == 'insert':
            draw.rectangle([right_x + GUTTER_WIDTH, y, total_width, y + LINE_HEIGHT], fill=ADD_BG_STRONG)
        elif diff_line.change_type == 'replace' and diff_line.right_text:
            draw.rectangle([right_x + GUTTER_WIDTH, y, total_width, y + LINE_HEIGHT], fill=ADD_BG)

        # Right text with syntax highlighting
        text_x = right_x + GUTTER_WIDTH + 6
        if diff_line.right_text:
            tokens = _tokenize_line(diff_line.right_text, lexer)
            for ttype, value in tokens:
                color = token_colors.get(ttype, FG_COLOR)
                while color == FG_COLOR and ttype.parent:
                    ttype = ttype.parent
                    color = token_colors.get(ttype, FG_COLOR)
                draw.text((text_x, y + 2), value, fill=color, font=font)
                text_x += len(value) * CHAR_WIDTH

        y += LINE_HEIGHT

    # Draw truncation notice
    if truncated:
        draw.text((PADDING, y + 4), "... diff truncated ...", fill=GUTTER_FG, font=font)

    # Save to buffer
    buffer = BytesIO()
    img.save(buffer, format='PNG', optimize=True)
    buffer.seek(0)
    return buffer
