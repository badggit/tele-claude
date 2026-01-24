import logging
from pathlib import Path

from PIL import Image

from config import PROJECTS_DIR

logger = logging.getLogger("tele-claude.utils")

# Maximum image dimension for Claude API (multi-image requests)
MAX_IMAGE_DIMENSION = 2000


def ensure_image_within_limits(image_path: str) -> str:
    """Resize image if it exceeds Claude API dimension limits.

    The Claude API limits images to 2000px per dimension in multi-image requests.
    If an oversized image gets into conversation history, it poisons the session
    causing all subsequent requests to fail.

    Args:
        image_path: Path to the image file

    Returns:
        Path to the (possibly resized) image. May be the same path if resized in-place,
        or the original path if no resize was needed.
    """
    try:
        with Image.open(image_path) as img:
            width, height = img.size

            if width <= MAX_IMAGE_DIMENSION and height <= MAX_IMAGE_DIMENSION:
                # Image is within limits
                return image_path

            # Calculate new dimensions preserving aspect ratio
            if width > height:
                new_width = MAX_IMAGE_DIMENSION
                new_height = int(height * (MAX_IMAGE_DIMENSION / width))
            else:
                new_height = MAX_IMAGE_DIMENSION
                new_width = int(width * (MAX_IMAGE_DIMENSION / height))

            logger.info(f"Resizing image from {width}x{height} to {new_width}x{new_height}: {image_path}")

            # Resize using high-quality resampling
            resized = img.resize((new_width, new_height), Image.Resampling.LANCZOS)

            # Save back to same path (overwrite)
            # Preserve format based on file extension
            resized.save(image_path, quality=95)

            return image_path

    except Exception as e:
        logger.error(f"Failed to resize image {image_path}: {e}")
        # Return original path - let API error handling deal with it
        return image_path


def get_project_folders() -> list[str]:
    """List directories in ~/Projects."""
    if not PROJECTS_DIR.exists():
        return []
    return sorted([
        d.name for d in PROJECTS_DIR.iterdir()
        if d.is_dir() and not d.name.startswith('.')
    ])
