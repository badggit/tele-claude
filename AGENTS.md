# AGENTS.md

## Project Overview
Multi-platform bot bridging Telegram/Discord to Claude Agent SDK sessions. Each topic/thread = one Claude session.

## Architecture
- `main.py` - Unified CLI entry point (`telegram`, `telegram --local`, `discord`)
- `platforms/telegram/runner.py` - Telegram bot runners (global and local modes)
- `platforms/telegram/handlers.py` - Telegram message/callback handlers
- `platforms/discord/runner.py` - Discord bot runner
- `platforms/discord/handlers.py` - Discord message handlers
- `session.py` - Claude SDK integration, message streaming, tool permissions
- `config.py` - Environment config (BOT_TOKEN, PROJECTS_DIR, browser settings)
- `logger.py` - Session logging
- `diff_image.py` - Syntax-highlighted edit diffs

## Key Patterns
- Sessions stored in `sessions: dict[int, ClaudeSession]` (keyed by thread_id)
- Permission system: `DEFAULT_ALLOWED_TOOLS` + persistent `tool_allowlist.json`
- Streaming responses with rate limiting to avoid Telegram flood control
- HTML formatting via mistune for Telegram messages

## When making changes
- Run `pytest` before committing
- Run `pyright` for type checking
- Keep Telegram message limits in mind (4000 chars max)

## Dependencies
- python-telegram-bot (async)
- discord.py (async)
- claude-agent-sdk
- mistune (markdown to HTML)
- Pillow (diff images)

