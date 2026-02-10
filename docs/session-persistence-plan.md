# Session Persistence Feature

## Problem

Bot restarts kill all sessions. The `session_id` from Claude SDK (used for multi-turn conversation resume) is stored only in memory. Users lose conversation context on every bot restart/crash.

## Solution

Persist `session_key → claude_session_id` mapping to disk. On restart, restore session_id so `ClaudeAgentOptions(resume=session_id)` can resume conversations.

---

## Data Schema

`.bot-sessions.json` (project root, gitignored):

```json
{
  "version": 1,
  "sessions": {
    "telegram:123:456": {
      "claude_session_id": "sess_abc123...",
      "cwd": "/Users/gavrix/Projects/tele-bot",
      "platform": "telegram",
      "created_at": 1769230091.303,
      "last_activity": 1769230227.832,
      "message_count": 15
    }
  }
}
```

---

## Files to Create/Modify

### 1. NEW: `core/session_store.py`

Persistence module with `SessionStore` class:

```python
class SessionStore:
    def load(self) -> None                    # Load from disk on startup
    def save(self) -> None                    # Atomic write (temp file + rename)
    def get(session_key) -> PersistedSession  # Lookup
    def update_session_id(...)                # Save after session_id capture
    def remove(session_key)                   # Clear on explicit close
    def cleanup_expired() -> int              # Remove sessions older than 7 days
```

Global singleton: `session_store = SessionStore()`

### 2. MODIFY: `core/types.py`

Add dataclass (after line 47):

```python
@dataclass
class PersistedSession:
    """Session metadata for persistence."""
    claude_session_id: str
    cwd: str
    platform: str
    created_at: float
    last_activity: float
    message_count: int = 0
```

### 3. MODIFY: `core/dispatcher.py`

In `__init__` (line 43):
```python
from core.session_store import session_store
# After self._session_lock = asyncio.Lock()
session_store.load()
session_store.cleanup_expired()
```

In `_create_session` (after line 100):
```python
# After claude_session = await listener.create_session(trigger, cwd)
persisted = session_store.get(trigger.session_key)
if persisted and hasattr(claude_session, 'session_id'):
    claude_session.session_id = persisted.claude_session_id
    _log.info("Restored session_id for %s", trigger.session_key)
```

### 4. MODIFY: `session.py`

After `session.session_id = message.session_id` (line 1131):

```python
from core.session_store import session_store
from core.types import make_session_key

# Persist session_id
platform_type = "discord" if session.chat_id == 0 else "telegram"
if platform_type == "telegram":
    session_key = make_session_key("telegram", chat_id=session.chat_id, thread_id=session.thread_id)
else:
    session_key = make_session_key("discord", channel_id=session.thread_id)

session_store.update_session_id(
    session_key=session_key,
    claude_session_id=message.session_id,
    cwd=session.cwd,
    platform=platform_type,
)
```

Handle resume failures (wrap the `async with ClaudeSDKClient` block):
- Catch exceptions mentioning "session" or "resume"
- Clear stored session_id, retry without resume
- Log warning

### 5. MODIFY: `.gitignore`

Add: `.bot-sessions.json`

---

## Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Storage format | JSON file | Follows `tool_allowlist.json` pattern, no new deps |
| Save timing | Immediate on session_id capture | Critical moment before potential crash |
| Restore timing | Lazy (on first message) | Platform clients don't exist at startup |
| Expiry | 7 days inactivity | SDK may reject very old sessions anyway |
| Resume failure | Fallback to fresh session | Graceful degradation |

---

## Error Handling

1. **Corrupt JSON**: Log warning, start with empty store
2. **Write failure**: Log error, continue (in-memory still works)
3. **Resume rejection by SDK**: Clear stored session_id, start fresh
4. **Missing cwd**: Skip restore, log warning

---

## Verification

1. Start bot, send message to create session
2. Check `.bot-sessions.json` contains entry with `claude_session_id`
3. Restart bot (`Ctrl+C` + `python main.py run`)
4. Send message to same thread/channel
5. Verify Claude has conversation context (ask "what did I just say?")
6. Check logs for "Restored session_id for ..." message

Run existing tests: `pytest tests/`

---

## Files Summary

| File | Action | Lines affected |
|------|--------|----------------|
| `core/session_store.py` | Create | ~120 lines |
| `core/types.py` | Add dataclass | +10 lines after line 47 |
| `core/dispatcher.py` | Add load/restore | +8 lines in __init__ and _create_session |
| `session.py` | Add persist call | +15 lines after line 1131 |
| `.gitignore` | Add entry | +1 line |
