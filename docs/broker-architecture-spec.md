# Dispatcher + Session Actors Architecture

## Overview

Refactor the bot from "transport-locked single process" to "dispatcher + session actors" architecture. The dispatcher handles all transport listeners and routes triggers to isolated session actors.

### Terminology

| Term | Pattern | Description |
|------|---------|-------------|
| **Dispatcher** | Message Router / Gateway | Routes triggers to sessions, doesn't store-and-forward |
| **SessionActor** | Actor Model | Isolated session with own state, message-based communication |
| **Trigger** | Event | Incoming message/action from any transport |
| **ReplyTarget** | Port (Hexagonal) | Platform-agnostic output interface |
| **TransportListener** | Adapter (Hexagonal) | Platform-specific input handler |

### Key Design Decision: Preemptive Interrupt

New messages **always interrupt** the current task. This is intentional:

1. **Safety**: No ESC key in chat - users need an immediate brake for runaway agents
2. **UX**: Short messages like "stop" should halt immediately
3. **Pattern**: Supervisor pattern from Erlang/OTP - human is the supervisor who can preempt

Flow: `New message → Interrupt current task → Queue message → Process`

This differs from pure Actor Model mailbox semantics where messages queue. Here, the human supervisor takes priority.

## Current Problems

1. **Transport lock-in**: `run_polling()` and `client.run()` block forever, can't run both
2. **Port conflicts**: Both transports try to bind task API to port 9111
3. **Global state soup**: `sessions` dict, `pending_permissions`, task factory all global
4. **Tight coupling**: `ClaudeSession` knows about platform clients directly

## Proposed Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         DISPATCHER                               │
│                                                                  │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐              │
│  │  Telegram   │  │   Discord   │  │  Task API   │              │
│  │  Listener   │  │  Listener   │  │  (HTTP)     │              │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘              │
│         │                │                │                      │
│         └────────────────┼────────────────┘                      │
│                          ▼                                       │
│                 ┌─────────────────┐                              │
│                 │  Session Router │                              │
│                 │                 │                              │
│                 │ (platform, id)  │                              │
│                 │      ↓          │                              │
│                 │   Session?      │                              │
│                 └────────┬────────┘                              │
│                          │                                       │
└──────────────────────────┼───────────────────────────────────────┘
                           │
         ┌─────────────────┼─────────────────┐
         ▼                 ▼                 ▼
   ┌───────────┐    ┌───────────┐    ┌───────────┐
   │  Session  │    │  Session  │    │  Session  │
   │  Worker   │    │  Worker   │    │  Worker   │
   │           │    │           │    │           │
   │ - Claude  │    │ - Claude  │    │ - Claude  │
   │   conv    │    │   conv    │    │   conv    │
   │ - Reply   │    │ - Reply   │    │ - Reply   │
   │   Target  │    │   Target  │    │   Target  │
   └───────────┘    └───────────┘    └───────────┘
```

## Core Concepts

### 1. Trigger

An incoming event that may start or continue a session.

```python
@dataclass
class Trigger:
    """Incoming event from any transport."""
    platform: str                    # Target platform: "telegram" | "discord"
    session_key: str                 # Unique composite key (see below)
    prompt: str                      # User message or injected task
    images: list[str] = field(default_factory=list)  # Image paths
    reply_context: dict = field(default_factory=dict)  # Info needed to create ReplyTarget
    source: str = "user"             # "user" | "task_api" | "cron" (for observability)
```

### Session Key Format

Composite key to avoid collisions (addresses Telegram non-topic chats, Discord DMs):

```python
def make_session_key(platform: str, **ids) -> str:
    """Generate collision-free session key."""
    if platform == "telegram":
        # chat_id required, thread_id optional (None for non-forum chats)
        chat_id = ids["chat_id"]
        thread_id = ids.get("thread_id")
        if thread_id:
            return f"telegram:{chat_id}:{thread_id}"
        return f"telegram:{chat_id}"

    elif platform == "discord":
        # channel_id required, guild_id for context
        channel_id = ids["channel_id"]
        return f"discord:{channel_id}"

    raise ValueError(f"Unknown platform: {platform}")
```

### 2. ReplyTarget (Protocol)

How a session sends messages back. Platform-agnostic interface with capability flags.

```python
@dataclass
class ReplyCapabilities:
    """What this reply target supports."""
    can_edit: bool = True          # Can edit sent messages
    can_buttons: bool = True       # Can send inline buttons
    can_typing: bool = True        # Can show typing indicator
    max_length: int = 4000         # Max message length
    max_buttons_per_row: int = 3   # Button layout constraint

@runtime_checkable
class ReplyTarget(Protocol):
    """Interface for sending replies back to the originating platform."""

    @property
    def capabilities(self) -> ReplyCapabilities:
        """What operations this target supports."""
        ...

    async def send(self, message: PlatformMessage) -> MessageRef:
        """Send a new message."""
        ...

    async def edit(self, ref: MessageRef, message: PlatformMessage) -> None:
        """Edit an existing message. Check capabilities.can_edit first."""
        ...

    async def send_buttons(
        self,
        message: PlatformMessage,
        buttons: list[ButtonRow]
    ) -> MessageRef:
        """Send with buttons. Check capabilities.can_buttons first."""
        ...

    async def typing(self) -> None:
        """Show typing. Check capabilities.can_typing first."""
        ...
```

### 3. SessionActor

Isolated unit that handles one conversation. Knows nothing about transports.
Uses internal mailbox for proper message serialization.

```python
@dataclass
class SessionActor:
    """Isolated session handling one Claude conversation."""
    session_key: str
    platform: str
    cwd: str
    reply_target: ReplyTarget
    claude_session: ClaudeAgentSession  # From SDK

    # Internal mailbox
    _mailbox: asyncio.Queue[Trigger] = field(default_factory=asyncio.Queue)
    _run_loop_task: Optional[asyncio.Task] = None
    _generation_id: int = 0  # Incremented on each new prompt, guards stale edits

    # State
    active: bool = True
    current_task: Optional[asyncio.Task] = None
    pending_permission: Optional[asyncio.Future] = None
    stats: SessionStats = field(default_factory=SessionStats)

    async def start(self) -> None:
        """Start the actor's run loop."""
        self._run_loop_task = asyncio.create_task(self._run_loop())

    async def enqueue(self, trigger: Trigger) -> None:
        """Add trigger to mailbox. Non-blocking, returns immediately."""
        await self._mailbox.put(trigger)

    async def _run_loop(self) -> None:
        """Main actor loop - processes mailbox sequentially."""
        while self.active:
            trigger = await self._mailbox.get()

            # Interrupt any running task
            if self.current_task and not self.current_task.done():
                self._generation_id += 1  # Invalidate stale edits
                self.stats.interrupt_count += 1
                await self._cancel_current_task()

            # Process new trigger
            self._generation_id += 1
            gen_id = self._generation_id
            self.current_task = asyncio.create_task(
                self._handle_prompt(trigger.prompt, trigger.images, gen_id)
            )

    async def _handle_prompt(self, prompt: str, images: list[str], gen_id: int) -> None:
        """Process a user prompt through Claude. gen_id guards stale operations."""
        self.stats.message_count += 1
        # Pass gen_id to streaming so edits check: if self._generation_id != gen_id: return
        ...

    async def _cancel_current_task(self) -> None:
        """Cancel current task and wait for cleanup."""
        if self.current_task:
            self.current_task.cancel()
            try:
                await self.current_task
            except asyncio.CancelledError:
                pass
        # Also cancel pending permission if any
        if self.pending_permission and not self.pending_permission.done():
            self.pending_permission.cancel()
            self.pending_permission = None

    async def resolve_permission(self, allowed: bool, always: bool) -> None:
        """Resolve a pending permission request."""
        ...

    async def close(self) -> None:
        """Clean up resources."""
        ...
```

### 4. Dispatcher

Central coordinator. Owns all listeners and routes triggers.
Uses lock to prevent duplicate session creation on simultaneous triggers.

```python
class Dispatcher:
    """Central coordinator for all transports and sessions."""

    def __init__(self):
        self._sessions: dict[str, SessionActor] = {}
        self._listeners: dict[str, TransportListener] = {}  # platform -> listener
        self._session_lock: asyncio.Lock = asyncio.Lock()
        self._task_api: Optional[TaskAPIServer] = None

    async def start(self) -> None:
        """Start all listeners and task API."""
        ...

    async def stop(self) -> None:
        """Graceful shutdown."""
        for session in self._sessions.values():
            await session.close()
        ...

    async def route_trigger(self, trigger: Trigger) -> None:
        """Route trigger to session. Enqueues and returns immediately (non-blocking)."""
        async with self._session_lock:
            if trigger.session_key not in self._sessions:
                session = await self._create_session(trigger)
                self._sessions[trigger.session_key] = session
                await session.start()  # Start actor run loop

        session = self._sessions[trigger.session_key]
        await session.enqueue(trigger)  # Non-blocking - just adds to mailbox

    async def _create_session(self, trigger: Trigger) -> SessionActor:
        """Create new session with appropriate ReplyTarget."""
        listener = self._listeners.get(trigger.platform)
        if not listener:
            raise ValueError(f"No listener for platform: {trigger.platform}")

        reply_target = listener.create_reply_target(trigger.reply_context)
        cwd = self._resolve_cwd(trigger)  # Project resolution logic

        return SessionActor(
            session_key=trigger.session_key,
            platform=trigger.platform,
            cwd=cwd,
            reply_target=reply_target,
            claude_session=ClaudeAgentSession(...),
        )

    def get_listener(self, platform: str) -> Optional[TransportListener]:
        """Get listener by platform name."""
        return self._listeners.get(platform)
```

### 5. TransportListener (Protocol)

Interface for platform-specific listeners.

```python
@runtime_checkable
class TransportListener(Protocol):
    """Interface for transport-specific message listeners."""

    async def start(self, on_trigger: Callable[[Trigger], Awaitable[None]]) -> None:
        """Start listening. Call on_trigger for each incoming event."""
        ...

    async def stop(self) -> None:
        """Stop listening."""
        ...

    def create_reply_target(self, trigger: Trigger) -> ReplyTarget:
        """Create a ReplyTarget for responding to this trigger."""
        ...
```

## Implementation Details

### Session Keys

Format: `{platform}:{thread_id}`

- Telegram: `telegram:12345678`
- Discord: `discord:98765432`
- Task API (new thread): `telegram:99999` or `discord:88888` (depends on which factory creates it)

### ReplyTarget Implementations

```python
class TelegramReplyTarget(ReplyTarget):
    """Sends replies to a Telegram thread."""
    def __init__(self, bot: Bot, chat_id: int, thread_id: int):
        self._client = TelegramClient(bot, chat_id, thread_id)

    async def send(self, message: PlatformMessage) -> MessageRef:
        return await self._client.send_message(message)

    # ... etc

class DiscordReplyTarget(ReplyTarget):
    """Sends replies to a Discord thread."""
    def __init__(self, channel: discord.Thread):
        self._client = DiscordClient(channel)

    # ... etc
```

### Listener Implementations

```python
class TelegramListener(TransportListener):
    """Listens for Telegram messages and converts to Triggers."""

    def __init__(self, bot_token: str, allowed_chats: set[int]):
        self._app: Optional[Application] = None
        self._on_trigger: Optional[Callable] = None

    async def start(self, on_trigger: Callable[[Trigger], Awaitable[None]]) -> None:
        self._on_trigger = on_trigger
        self._app = Application.builder().token(self._bot_token).build()

        # Register handlers that convert to Triggers
        self._app.add_handler(MessageHandler(
            filters.TEXT & filters.ChatType.SUPERGROUP,
            self._handle_message
        ))

        # Non-blocking start
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling()

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        thread_id = update.message.message_thread_id  # None for non-forum chats

        trigger = Trigger(
            platform="telegram",
            session_key=make_session_key("telegram", chat_id=chat_id, thread_id=thread_id),
            prompt=update.message.text,
            reply_context={
                "chat_id": chat_id,
                "thread_id": thread_id,
                "bot": context.bot,
            },
            source="user",
        )
        await self._on_trigger(trigger)
```

### Task API Integration

Task API is a trigger *source*, not a platform. Injected tasks must specify a target transport.

```python
class TaskAPIServer:
    """HTTP server for task injection."""

    async def handle_inject(self, request: web.Request) -> web.Response:
        payload = await request.json()

        # Option 1: Inject into existing session
        if "session_key" in payload:
            session = self._dispatcher.sessions.get(payload["session_key"])
            if not session:
                return web.json_response({"error": "session_not_found"}, status=404)
            trigger = Trigger(
                platform=session.platform,
                session_key=payload["session_key"],
                prompt=payload["prompt"],
                reply_context={},  # Session already has ReplyTarget
                source="task_api",
            )

        # Option 2: Create new session - MUST specify target
        else:
            platform = payload.get("platform")  # Required
            if platform not in ("telegram", "discord"):
                return web.json_response({"error": "platform_required"}, status=400)

            if platform == "telegram":
                # Required: chat_id. Optional: thread_id (creates new topic if missing)
                chat_id = payload.get("chat_id")
                thread_id = payload.get("thread_id")
                if not chat_id:
                    return web.json_response({"error": "chat_id_required"}, status=400)

                # If no thread_id, create new topic via listener
                if not thread_id:
                    listener = self._dispatcher.get_listener("telegram")
                    thread_id = await listener.create_topic(chat_id, payload.get("topic_name", "Task"))

                session_key = make_session_key("telegram", chat_id=chat_id, thread_id=thread_id)
                reply_context = {"chat_id": chat_id, "thread_id": thread_id}

            elif platform == "discord":
                channel_id = payload.get("channel_id")
                if not channel_id:
                    return web.json_response({"error": "channel_id_required"}, status=400)
                session_key = make_session_key("discord", channel_id=channel_id)
                reply_context = {"channel_id": channel_id}

            trigger = Trigger(
                platform=platform,
                session_key=session_key,
                prompt=payload["prompt"],
                reply_context=reply_context,
                source="task_api",
            )

        await self._dispatcher.route_trigger(trigger)
        return web.json_response({"status": "injected", "session_key": trigger.session_key})
```

**CLI Examples:**

```bash
# Inject into existing session
python main.py sessions inject --key "telegram:123:456" "Run tests"

# Create new session in Telegram chat (uses existing topic)
python main.py sessions inject \
    --platform telegram \
    --chat-id 123 \
    --thread-id 456 \
    "Daily report"

# Create new topic in Telegram chat
python main.py sessions inject \
    --platform telegram \
    --chat-id 123 \
    --topic-name "Nightly Build" \
    "Run full test suite"

# Inject into Discord channel
python main.py sessions inject \
    --platform discord \
    --channel-id 789 \
    "Check PR status"
```

## Entry Point

```python
# main.py
async def main():
    dispatcher = Dispatcher()

    # Add listeners based on available tokens
    if TELEGRAM_BOT_TOKEN:
        dispatcher.add_listener(TelegramListener(TELEGRAM_BOT_TOKEN, ALLOWED_CHATS))

    if DISCORD_BOT_TOKEN:
        dispatcher.add_listener(DiscordListener(DISCORD_BOT_TOKEN, ALLOWED_GUILDS))

    # Start task API (always, single port)
    dispatcher.start_task_api(TASK_API_HOST, TASK_API_PORT)

    # Run until interrupted
    await dispatcher.start()

    try:
        await asyncio.Event().wait()  # Run forever
    except KeyboardInterrupt:
        await dispatcher.stop()

if __name__ == "__main__":
    asyncio.run(main())
```

## Migration Path

Resequenced per Codex review to reduce refactoring risk.

### Phase 0: Foundation (No behavioral changes)
- Create `make_session_key()` function with composite key logic
- Create `Trigger` and `SessionStats` dataclasses
- Create `ReplyCapabilities` dataclass
- Add tests for session key generation across platforms
- **Risk**: None - purely additive

### Phase 1: Extract ReplyTarget
- Create `ReplyTarget` protocol with capability flags
- Wrap existing `TelegramClient`/`DiscordClient` as `ReplyTarget` implementations
- Update `ClaudeSession` to use `ReplyTarget` instead of `PlatformClient`
- Add capability checks before edit/buttons/typing calls
- **Test**: Existing bot still works with wrapped clients

### Phase 2: Create SessionActor
- Create `SessionActor` with internal mailbox queue
- Implement generation_id guards for stale edit prevention
- Wrap existing `ClaudeSession` inside `SessionActor`
- Route current handlers through SessionActor (still single-platform)
- **Test**: Interrupt behavior, generation_id guards

### Phase 3: Create Dispatcher skeleton
- Create `Dispatcher` class with session routing + lock
- Keep existing runners but have them call `dispatcher.route_trigger()`
- Update Task API to use new trigger format with target platform
- **Test**: Single-platform still works, Task API injection works

### Phase 4: Create Listeners
- Extract Telegram message handling into `TelegramListener`
- Extract Discord message handling into `DiscordListener`
- Listeners produce `Trigger` objects with proper session keys
- **Test**: Each platform works independently

### Phase 5: Unify entry point
- New `main.py` that starts dispatcher with all available listeners
- Single Task API instance shared across platforms
- Add CLI commands: `sessions list`, `sessions get`, `sessions inject`
- Remove old `run_global()`, `run_local()`, `run()` blocking functions
- **Test**: Multi-platform concurrent operation

### Phase 6: Observability & Polish
- Add SessionStats tracking throughout
- Implement session cleanup/GC policy
- Add structured logging for debugging
- Performance testing under load

## Benefits

1. **Multi-transport**: Telegram + Discord + Task API in one process
2. **Clean separation**: Sessions don't know about transports
3. **Testable**: Can test sessions with mock `ReplyTarget`
4. **Extensible**: Add new transports by implementing `TransportListener`
5. **Single task API**: No port conflicts
6. **Future-ready**: Could distribute sessions across processes/machines

## Observability

### Session Stats

Each session tracks stats for introspection:

```python
@dataclass
class SessionStats:
    """Runtime statistics for a session."""
    created_at: float                # time.time() at creation
    last_activity: float             # Updated on each message
    message_count: int = 0           # Total messages processed
    turn_count: int = 0              # Claude conversation turns
    interrupt_count: int = 0         # Times interrupted by user
    error_count: int = 0             # Errors encountered
```

### Session Info

Extended session payload for API/CLI:

```python
@dataclass
class SessionInfo:
    """Full session info for observability."""
    session_key: str                 # "telegram:12345"
    platform: str                    # "telegram" | "discord"
    thread_id: int                   # Platform-specific thread ID
    cwd: str                         # Working directory
    state: str                       # "idle" | "processing" | "awaiting_permission"
    stats: SessionStats

    # Computed
    @property
    def uptime_seconds(self) -> float:
        return time.time() - self.stats.created_at
```

### CLI Commands

Extend `main.py` with session management:

```
python main.py sessions list              # List all active sessions
python main.py sessions get <key>         # Get session details
python main.py sessions inject <key> <prompt>  # Inject into existing
python main.py sessions inject --new <prompt>  # Create new session
```

#### Implementation

```python
# main.py additions
def main() -> None:
    parser = argparse.ArgumentParser(...)
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Existing: telegram, discord
    ...

    # New: sessions
    sessions_parser = subparsers.add_parser("sessions", help="Manage sessions")
    sessions_sub = sessions_parser.add_subparsers(dest="action", required=True)

    # sessions list
    sessions_sub.add_parser("list", help="List active sessions")

    # sessions get <key>
    get_parser = sessions_sub.add_parser("get", help="Get session details")
    get_parser.add_argument("key", help="Session key (e.g., telegram:12345)")

    # sessions inject
    inject_parser = sessions_sub.add_parser("inject", help="Inject prompt")
    inject_parser.add_argument("--key", "-k", help="Session key (existing)")
    inject_parser.add_argument("--new", "-n", action="store_true", help="Create new session")
    inject_parser.add_argument("prompt", help="Prompt to inject")

    args = parser.parse_args()

    if args.command == "sessions":
        _handle_sessions(args)
    ...

def _handle_sessions(args) -> None:
    """Handle sessions subcommands via HTTP to running bot."""
    import httpx

    base_url = f"http://{TASK_API_HOST}:{TASK_API_PORT}"

    if args.action == "list":
        resp = httpx.get(f"{base_url}/sessions")
        sessions = resp.json()
        _print_sessions_table(sessions)

    elif args.action == "get":
        resp = httpx.get(f"{base_url}/sessions/{args.key}")
        if resp.status_code == 404:
            print(f"Session not found: {args.key}", file=sys.stderr)
            sys.exit(1)
        _print_session_detail(resp.json())

    elif args.action == "inject":
        payload = {"prompt": args.prompt}
        if args.key:
            payload["session_key"] = args.key
        elif args.new:
            payload["create_new"] = True
        resp = httpx.post(f"{base_url}/inject", json=payload)
        print(resp.json())
```

### Enhanced API Endpoints

```python
# task_api.py additions

async def handle_sessions(request: web.Request) -> web.Response:
    """GET /sessions - list all sessions with stats."""
    sessions_info = [
        {
            "session_key": s.session_key,
            "platform": s.platform,
            "thread_id": s.thread_id,
            "cwd": s.cwd,
            "state": _get_session_state(s),
            "uptime_seconds": time.time() - s.stats.created_at,
            "message_count": s.stats.message_count,
            "last_activity": s.stats.last_activity,
        }
        for s in dispatcher.sessions.values()
    ]
    return web.json_response(sessions_info)

async def handle_session_detail(request: web.Request) -> web.Response:
    """GET /sessions/{key} - get single session details."""
    key = request.match_info["key"]
    session = dispatcher.sessions.get(key)
    if not session:
        return web.json_response({"error": "not_found"}, status=404)

    return web.json_response({
        "session_key": session.session_key,
        "platform": session.platform,
        "thread_id": session.thread_id,
        "cwd": session.cwd,
        "state": _get_session_state(session),
        "stats": {
            "created_at": session.stats.created_at,
            "last_activity": session.stats.last_activity,
            "uptime_seconds": time.time() - session.stats.created_at,
            "message_count": session.stats.message_count,
            "turn_count": session.stats.turn_count,
            "interrupt_count": session.stats.interrupt_count,
            "error_count": session.stats.error_count,
        },
    })

def _get_session_state(session: SessionActor) -> str:
    """Determine current session state."""
    if session.pending_permission:
        return "awaiting_permission"
    if session.current_task and not session.current_task.done():
        return "processing"
    return "idle"
```

### CLI Output Format

```
$ python main.py sessions list

SESSION KEY        PLATFORM   STATE       UPTIME      MSGS   CWD
─────────────────────────────────────────────────────────────────────
telegram:12345     telegram   processing  2h 15m      42     /home/user/project
telegram:67890     telegram   idle        45m         8      /home/user/other
discord:11111      discord    awaiting    1h 30m      23     /var/projects/app

$ python main.py sessions get telegram:12345

Session: telegram:12345
Platform: telegram
State: processing
CWD: /home/user/project
Uptime: 2h 15m 32s
Messages: 42
Turns: 18
Interrupts: 3
Errors: 0
Last activity: 30s ago
```

## Open Questions

### Resolved

1. ~~**Session key collisions**~~ → Fixed: Composite key with chat_id + thread_id
2. ~~**Task API platform mismatch**~~ → Fixed: Task API requires target platform in payload
3. ~~**No actual queue**~~ → Fixed: SessionActor has internal mailbox + generation_id
4. ~~**Blocking dispatcher**~~ → Fixed: route_trigger enqueues and returns immediately
5. ~~**ReplyTarget capabilities**~~ → Fixed: Added ReplyCapabilities dataclass

### Still Open

1. **Permission handling**: How do button callbacks route back to sessions?
   - Option A: Dispatcher maintains callback_id → session_key mapping
   - Option B: Encode session_key in callback_id (preferred - stateless)

2. **Permission interruption policy**: What if new message arrives during pending permission?
   - Option A: Auto-deny the pending permission, process new message
   - Option B: Cancel pending permission (neither allow nor deny), process new message
   - Option C: Queue new message behind permission resolution
   - **Recommendation**: Option B - cancel cleanly, let user re-trigger if needed

3. **Session cleanup**: When to garbage collect idle sessions?
   - Timeout after N minutes of inactivity?
   - Explicit /close command?
   - LRU eviction with max session count?

4. **Error isolation**: If one session crashes, how to prevent affecting others?
   - Already addressed: Each SessionActor runs in its own asyncio task
   - Add try/except in run loop with error logging + stats.error_count

5. **Local mode**: How does `--local` fit in?
   - Separate `LocalTelegramListener` that always routes to one CWD?
   - Or config flag on `TelegramListener`?

6. **Session persistence**: In-memory only, or persist/recover across restarts?
   - Current: In-memory, sessions lost on restart (acceptable for MVP)
   - Future: Optional SQLite/Redis for session recovery
