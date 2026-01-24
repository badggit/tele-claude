import json
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# --- Telegram Config ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
PROJECTS_DIR = Path(os.getenv("PROJECTS_DIR", Path.home() / "Projects"))
GENERAL_TOPIC_ID = 0

# Authorized chat IDs - only these group chats can use the bot
# Set via ALLOWED_CHATS env var as comma-separated Telegram chat IDs
# Example: ALLOWED_CHATS=-1001234567890,-1009876543210
_allowed_chats_str = os.getenv("ALLOWED_CHATS", "")
ALLOWED_CHATS: set[int] = {
    int(cid.strip()) for cid in _allowed_chats_str.split(",") if cid.strip()
}

# --- Discord Config ---
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")

# Authorized Discord guild (server) IDs
# Set via DISCORD_ALLOWED_GUILDS env var as comma-separated IDs
_discord_guilds_str = os.getenv("DISCORD_ALLOWED_GUILDS", "")
DISCORD_ALLOWED_GUILDS: set[int] = {
    int(gid.strip()) for gid in _discord_guilds_str.split(",") if gid.strip()
}

# Discord channel -> project directory mapping
# Set via DISCORD_CHANNEL_PROJECTS env var as JSON: {"channel_id": "/path/to/project"}
# Example: DISCORD_CHANNEL_PROJECTS={"123456789": "/Users/me/Projects/myapp"}
_discord_projects_str = os.getenv("DISCORD_CHANNEL_PROJECTS", "{}")
try:
    _discord_projects_raw = json.loads(_discord_projects_str)
    DISCORD_CHANNEL_PROJECTS: dict[int, str] = {
        int(k): v for k, v in _discord_projects_raw.items()
    }
except (json.JSONDecodeError, ValueError):
    DISCORD_CHANNEL_PROJECTS = {}
