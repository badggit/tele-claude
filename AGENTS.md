# AGENTS.md

## Project Overview
Multi-platform bot bridging Telegram/Discord to Claude Agent SDK sessions. Each topic/thread = one Claude session.

## Quick Start

```bash
# Install
pip install -r requirements.txt

# Configure (minimal)
echo "BOT_TOKEN=your_telegram_token" > .env

# Run
python main.py telegram        # Telegram only
python main.py discord         # Discord only
python main.py run             # Both platforms
```

## CLI Reference

```bash
# Run bots
python main.py run                    # All available listeners
python main.py telegram               # Telegram only
python main.py telegram --local .     # Anchored to current directory
python main.py discord                # Discord only

# Session management (requires running bot)
python main.py sessions list          # List active sessions
python main.py sessions get <key>     # Get session details
python main.py sessions inject ...    # Inject prompt into session
```

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `BOT_TOKEN` | Yes (Telegram) | Telegram bot token |
| `DISCORD_BOT_TOKEN` | Yes (Discord) | Discord bot token |
| `PROJECTS_DIR` | No | Project root (default: ~/Projects) |
| `ALLOWED_CHATS` | No | Comma-separated Telegram chat IDs |
| `DISCORD_ALLOWED_GUILDS` | No | Comma-separated Discord guild IDs |

## Key Patterns

- **Preemptive interrupt**: New messages always interrupt current task (safety - no ESC key in chat)
- **Session keys**: `telegram:{chat_id}:{thread_id}` or `discord:{channel_id}`
- **Tool permissions**: `DEFAULT_ALLOWED_TOOLS` in session.py + `tool_allowlist.json`
- **Slash commands**: Global (`/help`, `/plan`, `/compact`) + project-specific from `commands/*.md`

## When Making Changes

- Run `pytest` before committing
- Run `pyright` for type checking
- Keep Telegram message limits in mind (4000 chars max)

## Architecture

See `docs/broker-architecture-spec.md` for detailed architecture documentation.

**Key components:**
- `core/dispatcher.py` - Routes triggers to session actors
- `core/session_actor.py` - Isolated session with mailbox queue
- `platforms/*/listener.py` - Platform-specific message handlers
- `task_api.py` - HTTP API for injection and observability

