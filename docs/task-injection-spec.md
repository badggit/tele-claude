# Task Injection API — Implementation Spec

## Goal

Add an HTTP API server to the bot that allows external processes (cron, scripts, curl) to inject tasks into the running bot. Each injected task triggers a full multi-turn Claude conversation, with output going to the platform (Telegram or Discord).

## Architecture

An `aiohttp` web server runs **inside the bot's existing event loop** (same process, same asyncio loop). It exposes a localhost-only HTTP API.

When a task is injected:
1. If `thread_id` is provided → inject into that existing session via `start_claude_task()`
2. If no `thread_id` → call the platform's **task channel factory** to create a new thread/topic, start a session, then inject

### Task Channel Factory

Each platform runner registers an async callback at startup:

- **Telegram:** Creates a new forum topic via `bot.create_forum_topic(chat_id, name)`, starts an ambient session in it (using `start_session_ambient()`), returns `thread_id`
- **Discord:** Finds the `#tasks` text channel in the guild, creates a new thread in it via `channel.create_thread(name=...)`, starts an ambient session (using `start_session_ambient_discord()`), returns the thread ID

The factory signature:
```python
CreateTaskChannel = Callable[[str], Awaitable[int]]
# Takes task_name (str), returns thread_id (int)
```

## New Files

### `task_api.py`

Core module. Contains:

1. **Factory registration:**
```python
_create_task_channel: Optional[Callable[[str], Awaitable[int]]] = None

def register_task_channel_factory(factory: Callable[[str], Awaitable[int]]) -> None:
    """Called by platform runners to register their channel factory."""
    global _create_task_channel
    _create_task_channel = factory
```

2. **HTTP handlers:**

- `POST /inject` — Main endpoint
  - Request body (JSON):
    - `prompt` (str, required): Task prompt for Claude
    - `thread_id` (int, optional): Target existing session. If omitted, creates new channel via factory.
    - `task_name` (str, optional): Name for new thread/topic. Defaults to first 50 chars of prompt.
  - Response 200: `{"status": "injected", "thread_id": <int>, "cwd": "<path>"}`
  - Response 400: Missing prompt or invalid JSON
  - Response 404: `thread_id` specified but no session found
  - Response 503: No task channel factory registered (bot not fully initialized)

- `GET /sessions` — List active sessions
  - Response 200: `[{"thread_id": <int>, "cwd": "<path>", "active": <bool>, "has_running_task": <bool>}]`

- `GET /health` — Health check
  - Response 200: `{"status": "ok", "sessions": <int>, "factory_registered": <bool>}`

3. **Server lifecycle:**
```python
async def start_task_api(host: str = ..., port: int = ...) -> web.AppRunner:
    """Start the HTTP server. Called from platform runners."""

async def stop_task_api() -> None:
    """Stop the HTTP server. Called on shutdown."""
```

Use `TASK_API_HOST` and `TASK_API_PORT` from config.py for defaults.

### `inject`

Executable CLI script (`chmod +x`). Zero external dependencies — uses only stdlib (`urllib.request`, `json`, `argparse`).

Usage:
```bash
./inject "Post the next Instagram photo"          # new thread, default
./inject --thread-id 12345 "Continue this task"   # existing session
./inject --task-name "Daily gram post" "Post the next photo from ~/gram-queue"
./inject --list-sessions
./inject --health
```

Arguments:
- `prompt` (positional, optional): Task prompt
- `--thread-id`, `-t` (int): Target existing session
- `--task-name` (str): Name for new thread/topic
- `--list-sessions`, `-l` (flag): List sessions
- `--health` (flag): Health check
- `--host` (str, default `127.0.0.1`): API host
- `--port` (int, default `9111`): API port

## Modified Files

### `config.py`

Add:
```python
# --- Task Injection API ---
TASK_API_HOST = os.getenv("TASK_API_HOST", "127.0.0.1")
TASK_API_PORT = _env_int("TASK_API_PORT", 9111)
```

(Remove the `INJECT_THREAD_ID` from the old plan — factory replaces it.)

### `platforms/telegram/runner.py`

In both `run_global()` and `run_local()`:

1. Add a `post_init` callback to the Application builder:
```python
async def _post_init(app: Application) -> None:
    from task_api import start_task_api, register_task_channel_factory
    from session import start_session_ambient

    bot = app.bot

    # Get chat_id from ALLOWED_CHATS (first one) for topic creation
    # If no ALLOWED_CHATS, task injection won't be able to create new topics
    from config import ALLOWED_CHATS
    if ALLOWED_CHATS:
        chat_id = next(iter(ALLOWED_CHATS))

        async def create_telegram_task_channel(task_name: str) -> int:
            topic = await bot.create_forum_topic(chat_id=chat_id, name=task_name)
            thread_id = topic.message_thread_id
            await start_session_ambient(chat_id, thread_id, bot)
            return thread_id

        register_task_channel_factory(create_telegram_task_channel)

    await start_task_api()

app = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()
```

2. For `run_local()`, same pattern but the factory uses the local project's chat_id.

### `platforms/discord/runner.py`

In `ClaudeBotClient.setup_hook()`:

```python
async def setup_hook(self):
    self._watchdog_task = self.loop.create_task(self._event_loop_watchdog())

    from task_api import start_task_api, register_task_channel_factory
    from session import start_session_ambient_discord

    bot_client = self

    async def create_discord_task_channel(task_name: str) -> int:
        # Find #tasks channel in any authorized guild
        tasks_channel = None
        for guild in bot_client.guilds:
            for channel in guild.text_channels:
                if channel.name == "tasks":
                    tasks_channel = channel
                    break
            if tasks_channel:
                break

        if not tasks_channel:
            raise RuntimeError("No #tasks channel found in any authorized guild")

        thread = await tasks_channel.create_thread(
            name=task_name,
            type=discord.ChannelType.public_thread,
        )
        await start_session_ambient_discord(thread.id, thread)
        return thread.id

    register_task_channel_factory(create_discord_task_channel)
    await start_task_api()
```

Note: `setup_hook` runs after login but before `on_ready`. The `self.guilds` list may not be populated yet at this point. If that's the case, defer factory registration to `on_ready` instead. Test this and adjust.

### `requirements.txt`

Add:
```
# Task injection API
aiohttp>=3.9.0
```

## Type Annotations

All new code must have full type annotations:
- Use `Callable[[str], Awaitable[int]]` for the factory type
- Use `Optional[...]` for nullable fields
- Use `web.Request` and `web.Response` for aiohttp handlers
- The `inject` CLI script should also be typed (it's simple enough)
- Run `pyright` and ensure zero errors in new files

## Tests

Create `tests/test_task_api.py` with unit tests:

1. **`test_health_endpoint`** — Start server, GET /health, verify response shape
2. **`test_sessions_endpoint_empty`** — GET /sessions with no sessions, verify empty list
3. **`test_sessions_endpoint_with_sessions`** — Mock sessions dict, GET /sessions, verify entries
4. **`test_inject_missing_prompt`** — POST /inject with no prompt, verify 400
5. **`test_inject_invalid_json`** — POST /inject with bad body, verify 400
6. **`test_inject_no_factory_no_thread`** — POST /inject without thread_id and no factory registered, verify 503
7. **`test_inject_existing_session`** — Mock a session in sessions dict, POST /inject with thread_id, verify start_claude_task called
8. **`test_inject_nonexistent_session`** — POST /inject with unknown thread_id, verify 404
9. **`test_inject_creates_channel`** — Register mock factory, POST /inject without thread_id, verify factory called and start_claude_task called
10. **`test_register_factory`** — Verify register/unregister works

Use `aiohttp.test_utils.AioHTTPTestCase` or `aiohttp.test_utils.TestClient` for testing the HTTP server without binding a real port.

Mock `session.sessions` dict and `session.start_claude_task` to avoid needing real Claude SDK.

## Implementation Order

1. `config.py` changes (small, no deps)
2. `task_api.py` (core logic)
3. `tests/test_task_api.py` (validate core logic)
4. `platforms/telegram/runner.py` (Telegram integration)
5. `platforms/discord/runner.py` (Discord integration)
6. `inject` CLI script
7. `requirements.txt` update
8. Run `pyright` on all new/modified files
9. Run `pytest` to verify tests pass

## Existing Code References

Key functions to import and use (do NOT modify these):
- `session.sessions: dict[int, ClaudeSession]` — active sessions dict
- `session.start_claude_task(thread_id: int, prompt: str, bot=None) -> Optional[asyncio.Task]` — inject work into session
- `session.start_session_ambient(chat_id, thread_id, bot) -> bool` — create ambient Telegram session
- `session.start_session_ambient_discord(channel_id, channel) -> bool` — create ambient Discord session
- `config.TASK_API_HOST`, `config.TASK_API_PORT` — server config
- `config.ALLOWED_CHATS` — authorized Telegram chat IDs (use first for topic creation)

## Notes

- The HTTP server MUST run in the same event loop as the bot (no separate thread/process)
- Localhost-only binding (127.0.0.1) — no auth needed
- The `inject` CLI must work with zero pip dependencies (stdlib only)
- Discord's `setup_hook` runs before `on_ready` — `self.guilds` may be empty. If so, move factory registration to `on_ready`
