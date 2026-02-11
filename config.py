import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# --- Dispatcher Config (shared across transports) ---
def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default

DISPATCHER_WORKERS = _env_int("DISPATCHER_WORKERS", 4)
DISPATCHER_MAX_QUEUE = _env_int("DISPATCHER_MAX_QUEUE", 1000)
DISPATCHER_QUEUE_WARN = _env_int("DISPATCHER_QUEUE_WARN", 200)

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


# --- Claude Model ---
# Set via CLAUDE_MODEL env var (e.g., claude-opus-4-6-20260205)
# Defaults to None which lets the SDK pick the default model
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL")

# --- Task Injection API ---
TASK_API_HOST = os.getenv("TASK_API_HOST", "127.0.0.1")
TASK_API_PORT = _env_int("TASK_API_PORT", 9111)
