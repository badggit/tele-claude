# Codex Integration: Lane-Switching Architecture

## Problem Statement

We want to spawn OpenAI Codex as a worker within existing Claude sessions, stream its output back to the user (tagged as Codex), and handle bidirectional communication — including the case where Codex asks the user a question mid-turn.

This is NOT "Codex as separate session." It's Codex as a **lane** within a Claude session: same chat thread, same user, but a different agent producing output.

## Prior Art & Inspiration

### CodexMonitor (by Dimillian)

[CodexMonitor](https://github.com/Dimillian/CodexMonitor) (4.5k+ stars) is a macOS Tauri app that orchestrates multiple Codex agents. It reverse-engineered / discovered the `codex app-server` stdio protocol and built a full GUI on top of it. This is how we discovered the protocol is stable enough for third-party use.

Key architectural choices from CodexMonitor:
- One `codex app-server` subprocess per workspace
- Rust backend handles stdio I/O, React frontend subscribes via Tauri IPC
- Thread discovery filters by workspace working directory
- `thread/resume` used to refresh messages from disk on selection

### OpenClaw (formerly Clawdbot/Moltbot)

[OpenClaw](https://github.com/openclaw/openclaw) is an open-source personal AI assistant by Peter Steinberger. Its **node architecture** inspired this design:

- **Gateway** = central WebSocket control plane that routes between messaging platforms and execution endpoints
- **Nodes** = remote devices (macOS/iOS/Android) that connect via WebSocket pairing and advertise capabilities (`system.run`, `camera.snap`, `screen.record`, etc.)
- **`node.invoke`** = dispatch a capability to a node with streaming results back
- **`sessions_send`** = agent-to-agent messaging for coordination (one agent can delegate to another and get results)
- **Tool streaming** = results stream back in real-time, not batched

The relevant pattern: OpenClaw's coding agent (Pi) receives tasks via the Gateway, executes them, and streams structured results (tool calls, outputs) back to the originating chat. Our Codex lane does the same thing but without the Gateway broker — we embed the subprocess directly in the session.

### Approaches Considered

| Approach | Status | Trade-offs |
|---|---|---|
| **tmux send-keys** | Rejected | Too slow. No structured output — just raw terminal text. No proper lifecycle management. |
| **Codex as separate session type** | Rejected | User wants Codex output in the *same* chat thread as Claude, with context handoff between agents. Separate sessions break this. |
| **Codex CLI fire-and-forget** | Rejected | `codex --full-auto "fix tests"` — no real-time structured streaming (just stdout blobs), no mid-turn user interaction, no proper interruption. Slightly better tmux. |
| **`codex mcp-server` as Claude tool** | **Candidate (v0/MVP)** | Simplest path. Zero custom protocol code. Claude calls Codex natively. But blocking — no streaming of intermediate work. See section below. |
| **App-server lane model** | **Candidate (v1)** | Full streaming, structured events, `turn/interrupt`, `tool/requestUserInput` forwarding, process reuse across turns. ~400 lines of custom code. |

### Option A: `codex mcp-server` (MVP / simplest path)

Codex has a built-in MCP server mode (`codex mcp-server`) that exposes two tools:

**`codex` tool** — start a new Codex session:
| Param | Type | Required | Purpose |
|---|---|---|---|
| `prompt` | string | Yes | The coding task |
| `approval-policy` | string | No | `"never"` / `"untrusted"` / `"on-request"` / `"on-failure"` |
| `sandbox` | string | No | `"read-only"` / `"workspace-write"` / `"danger-full-access"` |
| `cwd` | string | No | Working directory |
| `model` | string | No | Model override |
| `base-instructions` | string | No | Override default instructions |
| `config` | object | No | Override `config.toml` settings |

**`codex-reply` tool** — continue an existing session:
| Param | Type | Required | Purpose |
|---|---|---|---|
| `prompt` | string | Yes | Follow-up message |
| `threadId` | string | Yes | Thread ID from previous response |

**Response shape:**
```json
{
  "structuredContent": {
    "threadId": "019bbb20-bff6-7130-83aa-bf45ab33250e",
    "content": "Response text here"
  },
  "content": [{"type": "text", "text": "Response text here"}]
}
```

**Integration path:** Our bot already uses MCP servers (`create_sdk_mcp_server` in `mcp_tools.py` creates `telegram-tools`). We could spawn `codex mcp-server` as a stdio MCP server alongside `telegram-tools` in `ClaudeAgentOptions.mcp_servers`. Claude would then call Codex natively — no `/codex` command, no lane-switching, no custom NDJSON code.

**Advantages over app-server:**
- Zero custom protocol code
- Claude decides when to delegate (no explicit `/codex` command needed)
- `codex-reply` gives natural multi-turn conversations with Codex
- `threadId` persistence for free
- Context flows naturally — Claude sees Codex's response as a tool result

**Disadvantages:**
- **Blocking** — Claude freezes while Codex works (minutes). OpenAI examples set `client_session_timeout_seconds=360000` (100 hours!)
- **No streaming** — user sees `🔧 codex(fix the tests)` then nothing until done
- **No intermediate events** — no live command output, no file change notifications, no plan progress
- **No user interaction** — if Codex needs input, the MCP call just blocks or fails

**Verdict:** Good enough for MVP. User gets Codex delegation with almost no new code. The streaming limitation is real but tolerable for shorter tasks. Can upgrade to app-server lane model later.

### Option B: Codex TypeScript SDK with `runStreamed()`

The Codex TypeScript SDK (`@openai/codex-sdk`) supports streaming:
```typescript
import { Codex } from "@openai/codex-sdk";
const codex = new Codex();
const thread = codex.startThread();

// Blocking
const result = await thread.run("fix the tests");

// Streaming — async generator of structured events
const stream = await thread.runStreamed("fix the tests");
for await (const event of stream) {
  switch (event.type) {
    case "item.completed": /* ... */ break;
    case "turn.completed": /* ... */ break;
  }
}
```

**Problem:** TypeScript-only, no Python SDK. Would need a Node.js subprocess bridge, adding Node.js as a runtime dependency. Not worth it when app-server gives us the same events in Python.

### Option C: App-server lane model (full integration)

The rest of this document describes this approach. See "Architecture: Codex as a Lane" below.

## How the Codex App-Server Protocol Works

Codex ships with a `codex app-server` subcommand — a **JSON-RPC over stdio** interface (NDJSON: one JSON object per line). No HTTP, no sockets.

### Lifecycle

```
spawn `codex app-server` (subprocess, piped stdin/stdout)
  → initialize handshake
    → thread/start (create conversation)
      → turn/start (send user prompt, begin agent work)
        ← item/started, item/*/delta, item/completed (streaming)
        ← turn/completed (done)
      → turn/start (next prompt)
        ...
```

### Protocol Shape

**Requests (client → server):**
```json
{"method": "thread/start", "id": 1, "params": {"model": "gpt-5.1-codex", "cwd": "/path", "approvalPolicy": "never"}}
```

**Responses (server → client):**
```json
{"id": 1, "result": {"thread": {"id": "thr_abc", "preview": "...", "createdAt": 1234567890}}}
```

**Notifications (server → client, no `id`):**
```json
{"method": "item/agentMessage/delta", "params": {"itemId": "item_123", "text": "Here's what I'll do"}}
```

**Server-initiated requests (server → client, WITH `id` — client must respond):**
```json
{"method": "tool/requestUserInput", "id": 42, "params": {"questions": [{"question": "Which database?", "isOther": false}]}}
```

This last category is critical — it's how Codex asks the user questions.

### Key Protocol Methods

| Method | Direction | Purpose |
|---|---|---|
| `initialize` / `initialized` | client→server | Handshake (required first) |
| `thread/start` | client→server | Create conversation thread |
| `thread/resume` | client→server | Resume existing thread |
| `turn/start` | client→server | Send user input, begin agent work |
| `turn/interrupt` | client→server | Cancel in-flight turn |
| `item/commandExecution/requestApproval` | server→client | Ask to run a command |
| `item/fileChange/requestApproval` | server→client | Ask to edit a file |
| `tool/requestUserInput` | server→client | Ask user a question |

### Item Types (things Codex does during a turn)

| Item Type | Description | Claude Equivalent |
|---|---|---|
| `agentMessage` | Text output | `TextBlock` |
| `commandExecution` | Shell command + output | `ToolUseBlock(Bash)` + `ToolResultBlock` |
| `fileChange` | File create/modify/delete with diff | `ToolUseBlock(Edit/Write)` |
| `mcpToolCall` | MCP tool invocation | `ToolUseBlock(mcp__*)` |
| `reasoning` | Chain-of-thought | `ThinkingBlock` |
| `plan` | Structured plan with steps | No equivalent |
| `webSearch` | Web search queries | `ToolUseBlock(WebSearch)` |
| `imageView` | Image reference | No equivalent |
| `contextCompaction` | Context was compacted | `PreCompact` hook |

### Streaming Deltas

| Delta Event | What It Carries |
|---|---|
| `item/agentMessage/delta` | `{itemId, text}` — append to message |
| `item/commandExecution/outputDelta` | `{itemId, text}` — append to command output |
| `item/fileChange/outputDelta` | `{itemId, text}` — file operation output |
| `item/plan/delta` | `{itemId, text}` — append to plan |
| `item/reasoning/textDelta` | `{itemId, text}` — append to reasoning |
| `item/reasoning/summaryTextDelta` | `{itemId, summaryIndex, text}` — reasoning summary |

### Turn Completion

```json
{"method": "turn/completed", "params": {"turn": {"id": "turn_xyz", "status": "completed|interrupted|failed", "items": [...], "error": null}}}
```

Error shape when `status: "failed"`:
```json
{"error": {"message": "...", "codexErrorInfo": "ContextWindowExceeded|UsageLimitExceeded|...", "httpStatusCode": 429}}
```

### Additional Events

- `turn/diff/updated` — aggregated unified diff of all changes in current turn
- `turn/plan/updated` — structured plan with step statuses (`pending`/`inProgress`/`completed`)
- `thread/tokenUsage/updated` — token usage metrics

### Additional Item Types (lower priority for v1)

| Item Type | Description |
|---|---|
| `collabToolCall` | Agent-to-agent collaboration (sub-agent spawning) |
| `webSearch` | Web search with `search`, `openPage`, `findInPage` actions |
| `imageView` | Agent viewing an image file |
| `enteredReviewMode` / `exitedReviewMode` | Code review mode boundaries |
| `contextCompaction` | Context window being summarized |

### Additional Protocol Methods (not needed for v1 but available)

| Method | Purpose |
|---|---|
| `thread/fork` | Branch a thread into a new copy |
| `thread/read` | Fetch thread data without resuming into memory |
| `thread/rollback` | Drop last N turns from context |
| `review/start` | Run Codex reviewer on a thread (inline or detached) |
| `command/exec` | Run a single command under sandbox without creating a thread |
| `skills/list` | List available skills scoped by directory |
| `skills/config/write` | Enable/disable a skill |
| `model/list` | List available models with effort options |
| `account/rateLimits/read` | Check ChatGPT rate limits |
| `config/read` | Fetch effective configuration |

### Full `turn/start` Parameters

```json
{
  "threadId": "thr_abc",
  "input": [
    {"type": "text", "text": "fix the tests"},
    {"type": "localImage", "path": "/tmp/screenshot.jpg"},
    {"type": "image", "url": "https://..."},
    {"type": "skill", "name": "skill-name", "path": "/path/SKILL.md"}
  ],
  "model": "gpt-5.1-codex",
  "effort": "low|medium|high",
  "cwd": "/path/to/project",
  "approvalPolicy": "never|unlessTrusted",
  "sandboxPolicy": {
    "type": "workspaceWrite",
    "writableRoots": ["/path/to/project"],
    "networkAccess": true
  },
  "summary": "concise",
  "outputSchema": {}
}
```

Sandbox types: `dangerFullAccess`, `readOnly`, `workspaceWrite`, `externalSandbox`.

### Authentication Protocol

The `codex app-server` inherits auth from the host. Three modes:

1. **API Key**: `account/login/start` with `{"type": "apiKey", "apiKey": "sk-..."}` — immediate.
2. **ChatGPT Login**: `account/login/start` with `{"type": "chatgpt"}` — returns `authUrl` for browser-based OAuth. Server hosts callback at `localhost:<port>/auth/callback`. Emits `account/login/completed` notification on success.
3. **External ChatGPT Tokens**: `account/login/start` with `{"type": "chatgptAuthTokens", "idToken": "...", "accessToken": "..."}`. Server may later request refresh via `account/chatgptAuthTokens/refresh` (10s timeout).

Token refresh: if auth fails mid-turn, server sends `account/chatgptAuthTokens/refresh` as a server→client request. Client must respond with fresh tokens promptly.

### Schema Generation

The protocol is self-documenting:
```bash
codex app-server generate-json-schema --out ./schemas  # JSON Schema bundle
codex app-server generate-ts --out ./schemas            # TypeScript types
```

These match the running Codex version exactly. Useful for validating our client implementation.

---

## Architecture: Codex as a "Lane"

### Current Architecture

```
User message → handler → start_claude_task() → send_to_claude() → ClaudeSDK → platform.send_message()
```

All state lives in `ClaudeSession`. One session per thread.

### Proposed Architecture

```
User message → handler → routing logic
                              ├─ Normal message   → start_claude_task()  → send_to_claude()
                              ├─ /codex <task>     → start_codex_turn()   → send_to_codex()
                              └─ Codex waiting for → respond_to_codex()   → resolve user_input Future
                                 user input
```

### State Model

```python
@dataclass
class CodexLane:
    """Manages a Codex app-server subprocess within a Claude session."""
    process: asyncio.subprocess.Process
    reader_task: asyncio.Task
    codex_thread_id: str
    current_turn_id: Optional[str]          # Non-None = turn is active
    initialized: bool
    request_id_counter: int
    pending_responses: dict[int, asyncio.Future]  # For correlating request/response

    # Server-initiated request state (Codex asking us things)
    pending_user_input: Optional[PendingUserInput]  # Codex asked a question, waiting for user
    pending_approval: Optional[PendingApproval]      # Codex asked for approval, waiting for user

@dataclass
class PendingUserInput:
    """Codex sent tool/requestUserInput — we need the user's answer."""
    server_request_id: int    # The `id` from the server's request (we must respond with this id)
    questions: list[dict]      # The questions to display

@dataclass
class PendingApproval:
    """Codex sent requestApproval — we need user's decision."""
    server_request_id: int
    item_type: str             # "commandExecution" or "fileChange"
    details: dict              # command, cwd, reason, etc.
```

The `ClaudeSession` gets one new field:
```python
codex_lane: Optional[CodexLane] = None
```

### Session Mode

The session's "active mode" is determined by state, not an explicit flag:

| `codex_lane` | `codex_lane.current_turn_id` | `codex_lane.pending_user_input` | Mode |
|---|---|---|---|
| `None` | — | — | **Claude mode** (normal) |
| present | `None` | `None` | **Claude mode** (Codex idle, process alive for reuse) |
| present | `"turn_xyz"` | `None` | **Codex mode** (Codex working, messages are noise) |
| present | `"turn_xyz"` | present | **Codex-waiting mode** (next user message → Codex) |

---

## Message Routing (the hard part)

### Happy Path: `/codex fix the tests`

1. User sends `/codex fix the tests`
2. Handler recognizes `/codex` prefix, extracts task: "fix the tests"
3. If Claude is mid-turn: **interrupt Claude first** (existing `interrupt_session`)
4. If `codex_lane` is None: spawn process, initialize, create thread
5. Send `turn/start` with the task
6. Stream events back to chat, **every message tagged** (e.g., `"🔶 CODEX | <content>"` or a persistent header)
7. On `turn/completed`: clear `current_turn_id`, back to Claude mode

### Happy Path: Codex Asks a Question

1. Codex sends `tool/requestUserInput` with `id=42, questions=[{"question": "Which database?"}]`
2. We display: `"🔶 CODEX asks: Which database?"`
3. Set `codex_lane.pending_user_input = PendingUserInput(server_request_id=42, questions=[...])`
4. User sends "PostgreSQL"
5. Handler sees `pending_user_input` is set → routes to Codex, NOT Claude
6. We respond to server: `{"id": 42, "result": {"answers": ["PostgreSQL"]}}`
7. Clear `pending_user_input`
8. Codex continues its turn

### Happy Path: Codex Finishes, User Continues with Claude

1. Codex turn completes
2. `current_turn_id = None`, `pending_user_input = None`
3. User sends "now run the linter"
4. Handler sees no active Codex turn → routes to Claude as normal
5. Claude has full context of what happened (it saw the `/codex` command in its history)

Wait — **Claude doesn't have context of what Codex did.** This is a problem.

### The Context Gap Problem

When Codex runs, Claude's SDK is not involved. Claude doesn't see:
- What task Codex performed
- What files Codex changed
- What commands Codex ran
- What the output was

After Codex finishes, if the user says "now refactor that code," Claude has no idea what "that code" refers to.

**Options:**

1. **Inject a summary into Claude's next turn.** After Codex completes, compose a synthetic message summarizing what Codex did and feed it as context prefix to Claude's next `send_to_claude()` call.

2. **Don't care.** The user knows they're lane-switching. They can paste context or tell Claude what to look at. Codex changes are on disk — Claude can read them.

3. **Feed Codex's full output as a tool result.** Pretend `/codex` was a tool call that returned Codex's output. This requires fabricating SDK messages, which is fragile.

**Recommendation: Option 1 (summary injection).** When Codex turn completes, build a brief summary from the items:
```
[Codex completed: ran `npm test` (exit 0), modified src/auth.ts, created src/auth.test.ts]
```
Prepend this to the user's next Claude prompt as context.

---

## Edge Cases

### 1. User sends message while Codex is working (no pending_user_input)

Codex is mid-turn, actively running commands. User sends "actually never mind."

**Options:**
- (a) Interrupt Codex turn (`turn/interrupt`), route message to Claude
- (b) Queue message for Claude after Codex finishes
- (c) Ignore the message

**Recommendation: (a) Interrupt Codex.** Same UX as interrupting Claude — new message = cancel current work. Send `turn/interrupt`, wait for `turn/completed` with `status: "interrupted"`, then route to Claude.

### 2. User sends `/codex` while Claude is mid-turn

Claude is streaming a response. User sends `/codex deploy to staging`.

**Flow:**
1. Interrupt Claude (existing `interrupt_session`)
2. Wait for Claude to settle
3. Start Codex turn

This already works because `_handle_message_impl` interrupts before starting new work.

### 3. Multiple `/codex` commands in quick succession

User sends `/codex fix auth` then immediately `/codex fix tests`.

**Flow:**
1. First `/codex fix auth` starts a Codex turn
2. Second message arrives, sees Codex turn active, no `pending_user_input`
3. Per edge case #1: interrupt current Codex turn
4. Start new turn with "fix tests"

### 4. Codex process crashes mid-turn

`codex app-server` exits unexpectedly (segfault, OOM, etc.)

**Detection:** `_codex_reader_loop` sees EOF on stdout.

**Flow:**
1. Reader task detects EOF
2. Set turn as failed, notify user: `"🔶 CODEX process crashed"`
3. Clean up: `codex_lane.process = None`, `current_turn_id = None`
4. Next `/codex` command will respawn the process

Don't auto-restart — let user decide.

### 5. Codex sends approval request (with approvalPolicy != "never")

Even with `approvalPolicy: "never"`, future config changes might enable this.

**Flow:**
1. Codex sends `item/commandExecution/requestApproval` with `id=N`
2. Display approval buttons (reuse existing permission button UX)
3. User clicks Allow/Deny
4. Respond to server: `{"id": N, "result": {"decision": "accept"}}` or `"decline"`

This is almost identical to Claude's `request_tool_permission` flow. We can reuse the button callback pattern.

### 6. Codex turn times out

Codex runs for a very long time (complex refactor, stuck in loop).

**Mitigation:** No built-in timeout in the protocol. Options:
- Client-side timeout: if no events received for N minutes, send `turn/interrupt`
- Let user interrupt manually (same as any other message interrupting Codex)
- Warn user after N minutes of silence

**Recommendation:** Warn after 5 minutes of no events, auto-interrupt after 10.

### 7. Image input to Codex

User sends image + `/codex what's wrong with this screenshot?`

**Protocol support:** `turn/start` accepts `{"type": "localImage", "path": "/tmp/photo.jpg"}` input items. Works.

**Flow:** Same as Claude's `pending_image_path` pattern — prepend image path to Codex's input items.

### 8. Codex emits `reasoning` items

Equivalent to Claude's `ThinkingBlock`.

**Display:** Use the existing `platform.send_thinking()` method, tagged as Codex:
```
🔶 CODEX thinking: Let me analyze the test failures...
```

### 9. Codex emits `plan` items with step tracking

No Claude equivalent. Codex has structured plans:
```json
{"plan": [{"step": "Read test files", "status": "completed"}, {"step": "Fix failing assertions", "status": "inProgress"}, {"step": "Run tests", "status": "pending"}]}
```

**Display:** Render as a checklist, update via message edit:
```
🔶 CODEX plan:
  ✅ Read test files
  🔄 Fix failing assertions
  ⬜ Run tests
```

### 10. Rate limiting with dual agents

Both Claude and Codex generating messages → double the Telegram flood risk.

**Mitigation:** Codex messages go through the same `PlatformClient` which already rate-limits. The existing `send_interval` / `last_send` throttling applies. No special handling needed.

### 11. Codex `turn/diff/updated` events

Protocol sends aggregated diffs during the turn. We could render diff images like we do for Claude's Edit tool.

**Display:** On `turn/completed`, if there were file changes, generate diff images from the aggregated diff and send as gallery. Reuse `edit_to_image` / `send_diff_images_gallery`.

### 12. Authentication

The `codex app-server` inherits auth from the local Codex CLI installation (`~/.codex/auth.json` or OS credential store). If not authenticated, the process will fail on `initialize`.

**Prerequisite:** User must have `codex` installed and authenticated on the host machine.

**Error handling:** If `initialize` fails, surface clear error: `"Codex not authenticated. Run 'codex' in terminal to log in."`

### 13. Thread persistence across bot restarts

**Question:** Should we persist `codex_thread_id` so we can `thread/resume` after restart?

**Recommendation:** No, for v1. Just start fresh threads. The Codex process dies on bot restart anyway (it's a child process). Codex threads are cheap. Revisit if users want persistent Codex conversations.

### 14. Concurrent Codex turns across different sessions

Different chat threads each with their own Claude session each spawning their own Codex lane.

**No conflict:** Each `CodexLane` has its own subprocess. They're fully isolated. Multiple `codex app-server` processes can run simultaneously (each has its own thread state).

### 15. Codex `contextCompaction` item

Codex compacts its context mid-turn (like Claude's PreCompact).

**Display:** `"🔶 CODEX context compacting..."` — same pattern as Claude.

---

## Message Tagging Strategy

Every message originating from Codex needs visual distinction from Claude messages.

### Option A: Emoji prefix on every message
```
🔶 Here's what I found in the test files...
🔶 $ npm test
🔶 [~] src/auth.ts
```

**Pros:** Simple, clear
**Cons:** Noisy if Codex sends many messages, emoji takes space

### Option B: Header message + no per-message prefix
```
━━━ 🔶 CODEX STARTED: fix the tests ━━━
Here's what I found...
$ npm test
[~] src/auth.ts
━━━ 🔶 CODEX COMPLETED ━━━
```

**Pros:** Clean, minimal noise during Codex's work
**Cons:** In a long conversation, it's not immediately clear which messages are Codex

### Option C: Tool-name prefixing
```
🔧 codex::Bash(npm test)
🔧 codex::Edit(src/auth.ts)
```

**Pros:** Consistent with existing tool display format
**Cons:** Only works for tool calls, not text messages

### Recommendation: B + C combined

- Start/end banners for the Codex turn
- Tool calls prefixed with `codex::` (e.g., `codex::Bash`, `codex::Edit`)
- Text messages rendered normally between banners (they're already visually scoped)

---

## Protocol Comparison Summary

| Concern | Claude SDK | Codex App-Server | Compatible? |
|---|---|---|---|
| Transport | Library API | NDJSON stdio subprocess | Different |
| Text streaming | `TextBlock` in `AssistantMessage` | `item/agentMessage/delta` notifications | Translatable ✅ |
| Tool calls | `ToolUseBlock` | `commandExecution`, `fileChange`, `mcpToolCall` items | Translatable ✅ |
| Tool results | `ToolResultBlock` | Embedded in `item/completed` | Translatable ✅ |
| Thinking | `ThinkingBlock` | `reasoning` items | Translatable ✅ |
| Plans | N/A | `plan` items | New display needed |
| Web search | WebSearch ToolUseBlock | `webSearch` items | Translatable ✅ |
| Interruption | `client.interrupt()` | `turn/interrupt` request | Translatable ✅ |
| User questions | N/A (hooks) | `tool/requestUserInput` server→client request | New routing needed |
| Approvals | `can_use_tool` callback | `requestApproval` server→client request | Similar pattern ✅ |
| Image input | Image in prompt string | `localImage`/`image` in turn input array | Translatable ✅ |
| Diffs | Edit ToolUseBlock → `edit_to_image` | `turn/diff/updated` + `fileChange` items | Different but usable ✅ |
| Context compaction | PreCompact hook | `contextCompaction` item | Translatable ✅ |
| Session resumption | `session_id` in ResultMessage | `thread/resume` | Different mechanism |

**Bottom line:** The message shapes are fundamentally different, but every Codex event maps to an existing display pattern in our codebase. We need a translation layer (`_dispatch_codex_event`), not a protocol adapter.

---

## Open Questions

1. **Should `/codex` interrupt Claude or queue after Claude finishes?**
   Current recommendation: interrupt (consistent with how new messages already work).

2. **How much Codex context to inject into Claude's next turn?**
   Full item log? Summary only? Just a list of changed files?

3. **Should we persist the Codex subprocess across turns or spawn fresh each time?**
   Current recommendation: persist (reuse thread, avoid re-handshake overhead).

4. **Do we need approval support for v1, or is `approvalPolicy: "never"` sufficient?**
   Current recommendation: v1 = "never" only. Add approval support later.

5. **What model should Codex use?**
   Default to `gpt-5.1-codex` (or whatever's current). Make configurable via env var?

6. **Should Codex lane have its own `send_interval` / rate limit budget?**
   Current recommendation: share the session's rate limiter (both agents' messages go through same PlatformClient).

---

## File Impact Estimate

| File | Change Type | Scope |
|---|---|---|
| `codex_session.py` | **NEW** | ~400-500 lines: CodexLane, NDJSON protocol, process lifecycle, event dispatch, send_to_codex |
| `session.py` | Modify | Add `codex_lane` to ClaudeSession, routing in `start_claude_task`/`interrupt_session`, Codex summary injection |
| `config.py` | Modify | Add `CODEX_BINARY` env var |
| `platforms/telegram/handlers.py` | Modify | Recognize `/codex` command, route `pending_user_input` responses |
| `platforms/discord/handlers.py` | Modify | Same as Telegram handlers |

---

## Implementation Phases

### Phase 1: Core Protocol Client
- `CodexLane` dataclass
- NDJSON read/write layer
- Process spawn + initialize handshake
- Thread create
- Basic turn execution (send prompt, read events until turn/completed)
- Event → platform message translation (agentMessage, commandExecution, fileChange)

### Phase 2: Integration with Session
- `/codex` command recognition in handlers
- Interrupt-then-switch flow
- Message routing based on `pending_user_input` state
- Start/end banners
- Tool call tagging (`codex::Bash`, etc.)

### Phase 3: Bidirectional Communication
- `tool/requestUserInput` handling (display questions, route user response)
- `requestApproval` handling (if we move beyond `approvalPolicy: "never"`)

### Phase 4: Polish
- Codex summary injection into Claude's context
- `reasoning` / `plan` display
- `turn/diff/updated` → diff images
- Timeout warnings
- Error recovery (process crash → clean state)
- `turn/interrupt` on new message during Codex turn

---

## Feasibility Assessment

**Is this possible?** Yes, with caveats.

**What's straightforward:**
- Spawning and talking to `codex app-server` over stdio (well-documented protocol)
- Streaming events to chat (we already have all the display primitives)
- `/codex` command routing (simple string check in existing handlers)

**What's tricky but solvable:**
- `tool/requestUserInput` routing (need to intercept messages before they hit Claude)
- Interrupt flow (need to coordinate between Claude interrupt + Codex start)
- Codex summary injection (synthesizing context for Claude after Codex's turn)

**What could bite us:**
- The protocol is undocumented / semi-public — OpenAI could change it
- `codex app-server` might have undocumented behaviors we haven't accounted for
- Rate limiting under dual-agent load on Telegram
- The `tool/requestUserInput` feature is marked "experimental" in the protocol spec

**Confidence level: 8/10.** The core path works. The edge cases are manageable. The main risk is protocol stability, but CodexMonitor (4.5k stars) has been using it in production, so it's probably stable enough.

---

## Codex's Own Assessment (from querying Codex v0.93.0)

We asked Codex directly about the app-server approach. Key takeaways:

1. **Protocol stability:** The app-server is the official interface for rich clients (the VS Code extension uses it) and is open-source. No long-term wire compatibility promise — **pin the Codex version and regenerate schemas when upgrading.** Schemas can be generated per version via `codex app-server generate-json-schema`.

2. **Alternative:** Codex recommends the **Codex SDK for automation/CI** and the app-server for deep client integrations needing approvals, session management, or streamed events. For our use case (streaming + session management), app-server is the right choice. The `codex mcp-server` approach is the simpler alternative for non-streaming use.

3. **`turn/interrupt` reliability:** The turn ends with `status: "interrupted"` but **no guarantee that running commands are hard-killed.** Treat interrupts as best-effort. Use `externalSandbox` or `command/exec` with `timeoutMs` for strict termination control.

4. **`initialize` params:** Only `clientInfo.name` is required (for compliance logs). `title` and `version` are recommended but not required.

5. **Sandbox with `approvalPolicy: "never"`:** Use `workspaceWrite` for trusted/version-controlled projects. Avoid `dangerFullAccess`. Clients **must still handle server-initiated approval requests** even with "never" — user settings can override.

6. **Key gotcha:** `turn/diff/updated` can include empty items — rely on `item/*` events for item truth, not turn-level summaries.

---

## Lane Plugin Architecture (Phase 2)

The MVP baked codex support directly into core files (`session.py`, `config.py`, `handlers.py`). This section describes how to make it — and future agent integrations — fully pluggable.

### Goal

Adding or removing a lane = one import line. Zero core file changes per new lane.

### Current Coupling (what we're eliminating)

| Core file | Codex-specific code | Lines |
|---|---|---|
| `config.py` | `CODEX_BINARY` env var | 2 |
| `session.py` | `codex_lane` field + `start_codex_task()` | ~65 |
| `telegram/handlers.py` | hardcoded `/codex` if-block + import | ~28 |
| `discord/handlers.py` | **missing entirely** (bug) | 0 |

### LanePlugin Protocol

```python
# lanes/protocol.py

@dataclass
class LaneInput:
    """Structured input for a lane turn."""
    text: str
    image_paths: list[str] = field(default_factory=list)
    # Extensible — add attachments, metadata, etc. as needed


@runtime_checkable
class LanePlugin(Protocol):
    """Interface for an agent lane plugin."""

    command: str        # "codex" — no slash
    usage: str          # "Usage: /codex <task>"
    description: str    # "Run a task with OpenAI Codex"

    async def start(self, cwd: str, platform: PlatformClient, formatter: MessageFormatter) -> None:
        """Initialize the lane (spawn subprocess, handshake, etc).
        Called lazily on first invocation. Idempotent."""
        ...

    async def run_turn(self, input: LaneInput) -> Optional[str]:
        """Execute a turn. Returns optional summary for context injection."""
        ...

    async def interrupt(self) -> None:
        """Request graceful interruption of the current turn.
        Best-effort — may not immediately stop running commands.
        Safe to call when not busy (no-op)."""
        ...

    async def stop(self) -> None:
        """Shut down entirely. Safe to call multiple times."""
        ...

    @property
    def is_busy(self) -> bool:
        """Whether the lane is currently executing a turn."""
        ...

    @property
    def wants_user_input(self) -> bool:
        """Whether the lane is waiting for a user response (e.g., Codex tool/requestUserInput).
        When True, the next user message should be routed to handle_user_input()."""
        ...

    async def handle_user_input(self, text: str) -> None:
        """Forward a user message to the lane (answering a question it asked).
        Only called when wants_user_input is True."""
        ...
```

Uses `Protocol` (structural typing) — consistent with existing `PlatformClient` and `MessageFormatter`. No ABC inheritance needed.

Key additions from Codex review:
- **`LaneInput`** instead of bare `str` — carries text + images (matches `session.pending_image_path` pattern)
- **`interrupt()`** — lanes own their interrupt logic (e.g., Codex sends `turn/interrupt` to subprocess)
- **`wants_user_input` / `handle_user_input()`** — bidirectional routing for mid-turn questions

### Registry

```python
# lanes/__init__.py

_registry: dict[str, type[LanePlugin]] = {}

def register_lane(cls) -> cls:
    """Decorator to register a lane plugin class."""
    cmd = cls.command.lower()
    if cmd in _registry:
        raise ValueError(f"Lane command '{cmd}' already registered by {_registry[cmd].__name__}")
    _registry[cmd] = cls
    return cls

def get_lane_class(command: str) -> Optional[type[LanePlugin]]:
    """Look up a lane class by command name."""
    return _registry.get(command.lower())

def get_all_lanes() -> dict[str, type[LanePlugin]]:
    """Get all registered lanes (for /help display)."""
    return dict(_registry)

# --- Plugin discovery: explicit imports ---
# Each module self-registers via @register_lane decorator.
# To add a lane: add one import line. To remove: delete it.
from . import codex  # noqa: F401
```

No metaclass magic, no directory scanning, no entry points. Just explicit imports. Duplicate commands raise `ValueError` at import time.

### Dispatch (shared by both platforms)

```python
# lanes/dispatch.py

def parse_lane_command(text: str) -> Optional[tuple[str, str]]:
    """Check if text is a registered lane command.
    Returns (command_name, prompt) or None.
    Handles /cmd@botname syntax."""
    if not text.startswith("/"):
        return None
    command_part = text.split()[0].lstrip("/").split("@")[0]
    cls = get_lane_class(command_part)
    if cls is None:
        return None
    prompt = text.split(None, 1)[1].strip() if " " in text else ""
    return (command_part, prompt)


def get_active_lane(session) -> Optional[LanePlugin]:
    """Return the lane that should receive the next user message, if any.
    Checks all lanes for wants_user_input (lane asked a question)."""
    for lane in session.lanes.values():
        if lane.wants_user_input:
            return lane
    return None


def start_lane_task(session, command: str, input: LaneInput) -> Optional[asyncio.Task]:
    """Start a lane turn as a background task within a session.
    Lazy-initializes the lane if needed. Handles error recovery.
    Stores summary for Claude context injection."""
    cls = get_lane_class(command)
    if cls is None:
        return None

    async def _run():
        platform = session.get_platform()
        formatter = session.get_formatter()
        if not platform:
            return

        # Get or create lane instance
        lane = session.lanes.get(command)
        if lane is None:
            try:
                lane = cls()
                await lane.start(cwd=session.cwd, platform=platform, formatter=formatter)
                session.lanes[command] = lane
            except Exception as e:
                await platform.send_message(f"Failed to start {command}: {e}")
                return

        # Run turn
        try:
            summary = await lane.run_turn(input)
            # Persist summary for Claude context injection
            if summary:
                session.lane_context_prefix = summary
        except Exception as e:
            await platform.send_message(f"Lane {command} error: {e}")
            try:
                await lane.stop()
            except Exception:
                pass
            session.lanes.pop(command, None)
        finally:
            session.current_task = None
            session.active_lane = None

    task = asyncio.create_task(_run())
    session.current_task = task
    session.active_lane = command
    return task
```

### Session Changes

On `ClaudeSession`:
```python
# REMOVE:
codex_lane: Optional[Any] = None

# ADD:
lanes: dict[str, Any] = field(default_factory=dict)
# Maps command name -> LanePlugin instance, e.g. {"codex": <CodexLaneAdapter>}

active_lane: Optional[str] = None
# Which lane command owns session.current_task right now (None = Claude)

lane_context_prefix: Optional[str] = None
# Summary from last lane turn, injected into next Claude prompt then cleared
```

In `stop_session()` — clean up all active lanes:
```python
for lane_name, lane in list(session.lanes.items()):
    try:
        await lane.stop()
    except Exception:
        pass
session.lanes.clear()
```

In `interrupt_session()` — route interrupt to active lane or Claude:
```python
if session.active_lane:
    lane = session.lanes.get(session.active_lane)
    if lane and lane.is_busy:
        await lane.interrupt()
    # Then cancel the task as fallback (same as Claude path)
else:
    # Existing Claude interrupt logic (client.interrupt() + task cancel)
```

In `send_to_claude()` — inject lane context:
```python
if session.lane_context_prefix:
    prompt = f"{session.lane_context_prefix}\n\n{prompt}"
    session.lane_context_prefix = None
```

Delete `start_codex_task()` entirely — replaced by `lanes.dispatch.start_lane_task()`.

### Handler Changes

Both Telegram and Discord handlers replace hardcoded `/codex` blocks with identical generic dispatch.

**Priority 1 — Route replies to lanes waiting for user input:**
```python
from lanes.dispatch import parse_lane_command, start_lane_task, get_active_lane
from lanes.protocol import LaneInput

# FIRST CHECK: Is a lane waiting for the user's answer?
active = get_active_lane(session)
if active:
    await active.handle_user_input(text)
    return
```

This runs before any command parsing or Claude dispatch. If Codex asked "Which database?" and the user types "PostgreSQL", it goes to Codex, not Claude.

**Priority 2 — Lane slash commands:**
```python
# Check for lane commands (before regular slash command lookup)
lane_match = parse_lane_command(text)
if lane_match:
    command, lane_prompt = lane_match
    if not lane_prompt:
        cls = get_lane_class(command)
        await platform.send_message(cls.usage)
        return

    # Build structured input (text + buffered image if any)
    images = []
    if session.pending_image_path:
        images.append(session.pending_image_path)
        session.pending_image_path = None

    await interrupt_session(thread_id)
    start_lane_task(session, command, LaneInput(text=lane_prompt, image_paths=images))
    return
```

**Priority 3 — Regular messages go to Claude** (existing behavior, unchanged).

This replaces the 28-line hardcoded block with ~15 lines of generic dispatch. Also **fixes Discord** which currently has no `/codex` support.

### Codex Adapter

```python
# lanes/codex.py
from lanes import register_lane
from lanes.protocol import LaneInput

@register_lane
class CodexLaneAdapter:
    """Thin wrapper around existing codex_lane.py module."""
    command = "codex"
    usage = "Usage: /codex <task description>"
    description = "Run a task with OpenAI Codex"

    def __init__(self):
        self._lane = None

    async def start(self, cwd, platform, formatter):
        if self._lane is not None:
            return
        from codex_lane import start_codex_lane
        self._lane = await start_codex_lane(cwd=cwd, platform=platform, formatter=formatter)

    async def run_turn(self, input: LaneInput):
        from codex_lane import run_codex_turn
        # TODO: pass input.image_paths to Codex as localImage items in turn/start
        return await run_codex_turn(self._lane, input.text)

    async def interrupt(self):
        if self._lane is None or not self.is_busy:
            return
        from codex_lane import interrupt_codex_turn  # needs implementation in codex_lane.py
        await interrupt_codex_turn(self._lane)

    async def stop(self):
        if self._lane is None:
            return
        from codex_lane import stop_codex_lane
        await stop_codex_lane(self._lane)
        self._lane = None

    @property
    def is_busy(self):
        return self._lane is not None and self._lane.is_busy

    @property
    def wants_user_input(self):
        if self._lane is None:
            return False
        return self._lane.pending_user_input is not None

    async def handle_user_input(self, text):
        if self._lane is None or not self.wants_user_input:
            return
        from codex_lane import respond_to_user_input  # needs implementation in codex_lane.py
        await respond_to_user_input(self._lane, text)
```

`codex_lane.py` needs two new functions added: `interrupt_codex_turn()` (sends `turn/interrupt`) and `respond_to_user_input()` (responds to pending `tool/requestUserInput`). The existing `_handle_server_request` auto-skip logic would be replaced by storing the pending request for the adapter to resolve.

The adapter wraps the existing module with lazy imports. `codex_lane.py` core logic stays intact.

### File Layout

```
lanes/                    # NEW package
    __init__.py           # Registry + explicit plugin imports
    protocol.py           # LanePlugin Protocol definition
    dispatch.py           # parse_lane_command(), start_lane_task()
    codex.py              # CodexLaneAdapter (thin wrapper)

codex_lane.py             # UNCHANGED — existing Codex NDJSON client
```

### What Adding a New Lane Looks Like

```python
# lanes/aider.py
@register_lane
class AiderLane:
    command = "aider"
    usage = "Usage: /aider <task>"
    description = "Run a task with Aider"

    async def start(self, cwd, platform, formatter): ...
    async def run_turn(self, prompt): ...
    async def stop(self): ...
    @property
    def is_busy(self): ...
```

```python
# lanes/__init__.py — add one line:
from . import aider  # noqa: F401
```

Done. No handler, session, or config changes.

### What This Deliberately Does NOT Do

- No dynamic plugin scanning (explicit imports are clear and debuggable)
- No lane-specific config subsystem (lanes read their own env vars)
- No inter-lane communication (lanes are independent)
- No persistent lane state across bot restarts (lanes are ephemeral)
- No MCP integration

### Review Findings (from Codex v0.93.0)

We asked Codex to review this architecture. Six issues found, all addressed above:

| # | Issue | Severity | Fix |
|---|---|---|---|
| 1 | No inbound message routing for lanes awaiting user input — replies go to Claude, lane stalls | **Critical** | Added `wants_user_input` / `handle_user_input()` to protocol. Handler checks `get_active_lane()` before any dispatch. |
| 2 | No interrupt contract for lanes — `interrupt_session()` only interrupts Claude | **Critical** | Added `interrupt()` to protocol. `interrupt_session()` routes to `lane.interrupt()` when `active_lane` is set. |
| 3 | `run_turn()` summary dropped — context gap remains unsolved | **High** | `start_lane_task()` stores summary in `session.lane_context_prefix`. `send_to_claude()` injects it into next prompt. |
| 4 | Input model too narrow — `run_turn(str)` ignores images/attachments | **Medium** | Replaced with `run_turn(LaneInput)`. `LaneInput` carries text + image_paths. Handler drains `pending_image_path` into it. |
| 5 | Task ownership ambiguous — `session.current_task` shared without tracking who owns it | **Medium** | Added `session.active_lane: Optional[str]`. `interrupt_session()` checks this to route correctly. Cleared on task completion. |
| 6 | Silent command collisions in registry | **Low** | `register_lane()` raises `ValueError` on duplicate commands. Normalizes to lowercase. |

### Implementation Order

1. Create `lanes/protocol.py` — LanePlugin protocol + LaneInput dataclass
2. Create `lanes/__init__.py` — registry with collision detection
3. Create `lanes/codex.py` — adapter wrapping codex_lane.py (including new methods)
4. Create `lanes/dispatch.py` — command parsing, active lane detection, task lifecycle with summary persistence
5. Modify `codex_lane.py` — add `interrupt_codex_turn()`, `respond_to_user_input()`, `pending_user_input` field
6. Modify `session.py` — `lanes` dict, `active_lane`, `lane_context_prefix`, delete `start_codex_task()`, update `stop_session()` and `interrupt_session()`, inject context in `send_to_claude()`
7. Modify `telegram/handlers.py` — active lane check + generic lane dispatch
8. Modify `discord/handlers.py` — same lane dispatch (new feature)
9. Run `pyright` + `pytest`

### Verification

1. `pyright` — 0 errors
2. `pytest` — all tests pass
3. `/codex <task>` works in Telegram (same behavior as before)
4. `/codex <task>` works in Discord (new — was missing)
5. `/codex` without args shows usage
6. Session stop cleans up codex subprocess
7. Codex question → user reply routes to Codex, not Claude
8. New message during Codex turn → lane interrupted properly
9. Lane summary injected into next Claude prompt
