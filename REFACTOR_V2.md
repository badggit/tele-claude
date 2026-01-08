# Multi-Platform Refactor (v2)

**Branch:** `feature/multi-platform-v2`
**Started:** 2026-01-07
**Goal:** Abstract Telegram-specific code to support Discord (and future platforms)

---

## Quick Resume Instructions

If context is lost, read this file and the plan at:
`~/.claude/plans/flickering-greeting-sonnet.md`

Then continue from the current phase marked below.

---

## Architecture Overview

```
session.py (platform-agnostic Claude SDK logic)
    ↓ uses
platforms/protocol.py (PlatformClient, MessageFormatter protocols)
    ↓ implemented by
platforms/telegram/client.py   OR   platforms/discord/client.py
```

**Key changes:**
- `ClaudeSession.bot: Bot` → `ClaudeSession.platform: PlatformClient`
- `TelegramHTMLRenderer` moves to `platforms/telegram/formatter.py`
- Discord: channel_id = project directory (no folder picker)

---

## Phases & Progress

### Phase 1: Protocol Layer
- [ ] Create `platforms/__init__.py`
- [ ] Create `platforms/protocol.py` with `PlatformClient`, `MessageFormatter`, `ButtonSpec`, `MessageRef`

### Phase 2: Extract Telegram Client
- [ ] Create `platforms/telegram/__init__.py`
- [ ] Create `platforms/telegram/client.py` (TelegramClient wrapping Bot)
- [ ] Create `platforms/telegram/formatter.py` (move TelegramHTMLRenderer from session.py)
- [ ] Refactor `session.py`:
  - [ ] Change ClaudeSession.bot → platform
  - [ ] Add ClaudeSession.formatter
  - [ ] Replace bot.send_message → platform.send_message
  - [ ] Replace bot.edit_message_text → platform.edit_message
  - [ ] Update request_tool_permission to use ButtonSpec
- [ ] Test Telegram still works

### Phase 3: Extract Telegram Handlers
- [ ] Create `platforms/telegram/handlers.py` (from handlers.py)
- [ ] Create `platforms/telegram/bot.py` (from bot.py)
- [ ] Update imports in handlers.py
- [ ] Test Telegram still works

### Phase 4: Discord Implementation
- [ ] Create `platforms/discord/__init__.py`
- [ ] Create `platforms/discord/client.py` (DiscordClient)
- [ ] Create `platforms/discord/formatter.py` (DiscordFormatter - markdown passthrough)
- [ ] Create `platforms/discord/handlers.py` (on_message, on_interaction)
- [ ] Create `platforms/discord/bot.py` (entry point)
- [ ] Add Discord config to config.py
- [ ] Test Discord bot

### Phase 5: MCP Tools Update
- [ ] Update mcp_tools.py to use platform.send_document
- [ ] Test file sending on both platforms

### Phase 6: Cleanup
- [ ] Remove deprecated code
- [ ] Update pyproject.toml with discord.py dependency
- [ ] Update tests
- [ ] Final testing

---

## Current Status

**Phase:** 4 COMPLETE ✓
**Last action:** Implemented Discord platform support
**Next step:** Phase 5 - Update MCP tools for platform abstraction

### Completed
- [x] Phase 1: Protocol definitions (PlatformClient, MessageFormatter, etc.)
- [x] Phase 2: Telegram client extraction + session.py refactor
  - [x] TelegramClient and TelegramFormatter extracted
  - [x] ClaudeSession updated with platform/formatter fields
  - [x] request_tool_permission uses platform
  - [x] pre_compact_hook uses platform
  - [x] send_typing_action uses platform
  - [x] send_diff_images_gallery uses platform
  - [x] send_message returns MessageRef, uses platform
  - [x] send_or_edit_response uses MessageRef, platform
  - [x] _send_with_fallback uses platform
  - [x] send_to_claude streaming loop uses MessageRef throughout
  - [x] Tests updated and passing (59/59)
  - [x] Pyright clean (0 errors)
- [x] Phase 3: Handlers reorganization
  - [x] handlers.py moved to platforms/telegram/handlers.py
  - [x] bot.py and bot_local.py updated to use new import path
  - [x] Tests passing, pyright clean
- [x] Phase 4: Discord platform implementation
  - [x] platforms/discord/client.py (DiscordClient)
  - [x] platforms/discord/formatter.py (DiscordFormatter - markdown)
  - [x] platforms/discord/handlers.py (message, attachment, interaction handlers)
  - [x] run_discord.py entry point
  - [x] Discord config in config.py (DISCORD_BOT_TOKEN, channel->project mapping)
  - [x] start_session_discord in session.py
  - [x] Tests passing, pyright clean

---

## Key Files Reference

| File | Lines | Role |
|------|-------|------|
| `session.py` | 1232 | Core Claude SDK integration - HEAVY REFACTOR |
| `handlers.py` | 371 | Telegram handlers - MOVE to platforms/telegram/ |
| `bot.py` | 59 | Entry point - MOVE to platforms/telegram/ |
| `mcp_tools.py` | 168 | MCP tool - UPDATE to use platform |

---

## Git Strategy

- Work on `feature/multi-platform-v2` branch
- Commit after each sub-phase completion
- Stashed prior work: `git stash list` to see

---

## Rollback

If things go wrong:
```bash
git checkout main
git stash pop  # restore browser_tools/session tweaks
```
