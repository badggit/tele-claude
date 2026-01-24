"""
Claude Code SDK session management for Telegram bot.

Manages conversations with Claude through the Code SDK,
streaming responses to Telegram messages.

Supports multiple platforms via PlatformClient abstraction.
"""
import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Optional, Any, Union

import mistune
from telegram import Bot, Message, InputMediaPhoto, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction

from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, ProcessError, PermissionResultAllow, PermissionResultDeny, HookMatcher, HookContext
from claude_agent_sdk.types import (
    SystemPromptPreset, ToolPermissionContext, HookInput, HookJSONOutput,
    UserMessage, AssistantMessage, ResultMessage, TextBlock, ToolUseBlock, ThinkingBlock
)

from config import PROJECTS_DIR
from logger import SessionLogger
from diff_image import edit_to_image
from commands import load_contextual_commands, register_commands_for_chat
from mcp_tools import create_telegram_mcp_server

# Platform abstraction imports
from platforms import PlatformClient, MessageFormatter, ButtonSpec, ButtonRow, MessageRef
from platforms.telegram import TelegramClient, TelegramFormatter

# Check if Discord support is available
try:
    import discord
    DISCORD_AVAILABLE = True
except ImportError:
    DISCORD_AVAILABLE = False

# Module logger (named _log to avoid collision with SessionLogger variables named 'logger')
_log = logging.getLogger("tele-claude.session")


# Context window sizes by model (tokens)
MODEL_CONTEXT_WINDOWS = {
    "claude-opus-4-5-20251101": 200000,
    "claude-sonnet-4-5-20251101": 200000,
    "claude-sonnet-4-20250514": 200000,
    "default": 200000,
}

# Warn user when context remaining drops below this percentage
CONTEXT_WARNING_THRESHOLD = 15

# Tools that are always allowed without prompting
DEFAULT_ALLOWED_TOOLS = [
    "Read", "Write", "Edit", "Bash", "Glob", "Grep", "Task", "WebSearch",
    "Skill",  # Enable skills from .claude/skills/
    "mcp__telegram-tools__send_to_telegram",  # Custom tool for sending files to chat
]

# Persistent allowlist file
ALLOWLIST_FILE = Path(__file__).parent / "tool_allowlist.json"

# Pending permission requests: request_id -> (Future, SessionLogger)
pending_permissions: dict[str, tuple[asyncio.Future, Optional["SessionLogger"]]] = {}


def load_allowlist() -> set[str]:
    """Load the persistent tool allowlist."""
    if ALLOWLIST_FILE.exists():
        try:
            with open(ALLOWLIST_FILE, "r") as f:
                return set(json.load(f))
        except Exception:
            pass
    return set()


def save_allowlist(tools: set[str]) -> None:
    """Save the persistent tool allowlist."""
    try:
        with open(ALLOWLIST_FILE, "w") as f:
            json.dump(list(tools), f)
    except Exception:
        pass


def add_to_allowlist(tool_name: str) -> None:
    """Add a tool to the persistent allowlist."""
    tools = load_allowlist()
    tools.add(tool_name)
    save_allowlist(tools)


def is_tool_allowed(tool_name: str) -> bool:
    """Check if a tool is in the default or persistent allowlist."""
    if tool_name in DEFAULT_ALLOWED_TOOLS:
        return True
    return tool_name in load_allowlist()


async def resolve_permission(request_id: str, allowed: bool, always: bool = False, tool_name: Optional[str] = None) -> bool:
    """Resolve a pending permission request."""
    entry = pending_permissions.pop(request_id, None)
    if entry is None:
        _log.warning(f"Permission future not found for request_id={request_id}, pending_keys={list(pending_permissions.keys())}")
        return False

    future, logger = entry

    if logger:
        logger.log_permission_resolved(request_id, allowed, found=True)

    if always and tool_name:
        add_to_allowlist(tool_name)

    future.set_result(allowed)
    return True


@dataclass
class ClaudeSession:
    """Represents an active Claude session for a chat thread.

    Supports both legacy Telegram-specific mode (via bot field) and
    platform-agnostic mode (via platform field).
    """
    chat_id: int
    thread_id: int
    cwd: str
    # Platform abstraction (preferred)
    platform: Optional[PlatformClient] = None
    formatter: Optional[MessageFormatter] = None
    # Legacy Telegram support (deprecated - use platform instead)
    bot: Optional[Bot] = None  # Will be removed in future version
    # Session state
    logger: Optional[SessionLogger] = None
    last_send: float = field(default_factory=time.time)
    send_interval: float = 1.0
    last_typing_action: float = 0.0
    active: bool = True
    session_id: Optional[str] = None  # For multi-turn conversation
    client: Optional[ClaudeSDKClient] = None  # Active SDK client for interrupt support
    last_context_percent: Optional[float] = None  # Last known context remaining %
    pending_image_path: Optional[str] = None  # Buffered image waiting for prompt
    contextual_commands: list = field(default_factory=list)  # Project-specific slash commands

    def get_platform(self) -> Optional[PlatformClient]:
        """Get platform client, creating from bot if needed (backwards compat)."""
        if self.platform:
            return self.platform
        if self.bot:
            # Create TelegramClient wrapper for legacy bot
            self.platform = TelegramClient(
                bot=self.bot,
                chat_id=self.chat_id,
                thread_id=self.thread_id,
                logger=self.logger,
            )
            return self.platform
        return None

    def get_formatter(self) -> MessageFormatter:
        """Get message formatter, defaulting to Telegram."""
        if self.formatter:
            return self.formatter
        # Default to Telegram formatter for backwards compat
        self.formatter = TelegramFormatter()
        return self.formatter


async def interrupt_session(thread_id: int) -> bool:
    """Interrupt the active Claude response for a session.

    Returns True if an active query was interrupted, False otherwise.
    """
    session = sessions.get(thread_id)
    if not session or not session.client:
        return False

    await session.client.interrupt()
    return True


async def request_tool_permission(
    session: ClaudeSession,
    tool_name: str,
    tool_input: dict
) -> bool:
    """Send permission request and wait for user response.

    Uses platform abstraction for cross-platform support.
    """
    platform = session.get_platform()
    if platform is None:
        if session.logger:
            session.logger.log_error("request_tool_permission", Exception("No platform client available"))
        return False

    formatter = session.get_formatter()

    # Generate unique request ID
    request_id = str(uuid.uuid4())[:8]

    # Format tool input for display
    input_preview = []
    for k, v in list(tool_input.items())[:3]:  # Show first 3 args
        v_str = str(v)
        if len(v_str) > 100:
            v_str = v_str[:100] + "..."
        v_str = formatter.escape_text(v_str)
        input_preview.append(f"  {formatter.code(k)}: {v_str}")
    input_text = "\n".join(input_preview) if input_preview else "  (no arguments)"

    # Build message text using formatter
    message_text = (
        f"🔐 {formatter.bold('Permission Request')}\n\n"
        f"Tool: {formatter.code(tool_name)}\n"
        f"Arguments:\n{input_text}"
    )

    # Build platform-agnostic keyboard
    buttons = [
        ButtonRow([
            ButtonSpec("✅ Allow", f"perm:allow:{request_id}:{tool_name}"),
            ButtonSpec("❌ Deny", f"perm:deny:{request_id}:{tool_name}"),
        ]),
        ButtonRow([
            ButtonSpec("✅ Always Allow", f"perm:always:{request_id}:{tool_name}"),
        ])
    ]

    # Send permission request message
    await platform.send_message(message_text, buttons=buttons)

    # Log the permission request
    if session.logger:
        session.logger.log_permission_request(request_id, tool_name, tool_input)

    # Create future and wait for response
    # Use get_running_loop() - get_event_loop() is deprecated and may return wrong loop
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    pending_permissions[request_id] = (future, session.logger)

    if session.logger:
        session.logger.log_debug("permission", f"Waiting for user response", request_id=request_id, pending_keys=list(pending_permissions.keys()))

    try:
        # Wait for user response (timeout after 5 minutes)
        allowed = await asyncio.wait_for(future, timeout=300.0)
        if session.logger:
            session.logger.log_debug("permission", f"Got user response: {allowed}", request_id=request_id)
        return allowed
    except asyncio.TimeoutError:
        pending_permissions.pop(request_id, None)
        if session.logger:
            session.logger.log_debug("permission", "Request timed out", request_id=request_id)
        if platform:
            await platform.send_message("⏰ Permission request timed out (denied)")
        return False


def create_permission_handler(session: ClaudeSession):
    """Create a can_use_tool callback for the given session."""
    async def handle_permission(
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolPermissionContext
    ) -> Union[PermissionResultAllow, PermissionResultDeny]:
        try:
            in_allowlist = is_tool_allowed(tool_name)

            if session.logger:
                session.logger.log_permission_check(tool_name, in_allowlist)

            # Check if tool is in allowlist
            if in_allowlist:
                if session.logger:
                    session.logger.log_debug("permission_handler", f"Returning PermissionResultAllow (allowlist)")
                return PermissionResultAllow(updated_input=tool_input)

            # Request permission from user
            allowed = await request_tool_permission(session, tool_name, tool_input)

            if allowed:
                if session.logger:
                    session.logger.log_debug("permission_handler", f"Returning PermissionResultAllow (user allowed)")
                return PermissionResultAllow(updated_input=tool_input)
            else:
                if session.logger:
                    session.logger.log_debug("permission_handler", f"Returning PermissionResultDeny (user denied)")
                return PermissionResultDeny(message=f"User denied permission for {tool_name}")
        except Exception as e:
            if session.logger:
                session.logger.log_error("permission_handler", e)
            # Return deny on error
            return PermissionResultDeny(message=f"Permission error: {str(e)}")

    return handle_permission


def create_pre_compact_hook(session: ClaudeSession):
    """Create a PreCompact hook to log and notify user when context is being compacted."""
    async def handle_pre_compact(
        input_data: HookInput,
        tool_use_id: Optional[str],
        context: HookContext
    ) -> HookJSONOutput:
        """Called before the SDK compacts conversation history."""
        try:
            # Log the compaction event with full input data for analysis
            if session.logger:
                # Cast to dict for logging since HookInput is a TypedDict
                session.logger.log_compact_event(dict(input_data))  # type: ignore[arg-type]

            # Notify user in chat using platform abstraction
            platform = session.get_platform()
            if platform:
                formatter = session.get_formatter()
                await platform.send_message(
                    f"📦 {formatter.bold('Context compacting...')}\n"
                    "Conversation history is being summarized to free up space."
                )
        except Exception as e:
            if session.logger:
                session.logger.log_error("pre_compact_hook", e)

        # Return async hook output to allow compaction to proceed
        return {"async_": True}

    return handle_pre_compact


# Active sessions: thread_id -> ClaudeSession
sessions: dict[int, ClaudeSession] = {}

# Minimum seconds between sends to avoid flood control
MIN_SEND_INTERVAL = 1.0

# Typing action expires after ~5s, resend every 4s
TYPING_ACTION_INTERVAL = 4.0


def calculate_context_remaining(usage: Optional[dict[str, Any]], model: str = "default") -> Optional[float]:
    """Calculate percentage of context window remaining from usage data.

    Returns percentage remaining (0-100), or None if usage data insufficient.

    Note: We intentionally exclude cache_read_input_tokens from the calculation.
    The cache_read tokens appear to include accumulated reads from server-side
    tools (like web_search, web_fetch) that don't represent actual conversation
    context. The official docs confirm this issue:
    https://platform.claude.com/docs/en/build-with-claude/context-editing#client-side-compaction-sdk

    "When using server-side tools, the SDK may incorrectly calculate token usage...
    the cache_read_input_tokens value includes accumulated reads from multiple
    internal API calls made by the server-side tool, not your actual conversation
    context."

    We'll revisit this calculation when we observe an actual PreCompact event
    and can correlate the token counts with real context exhaustion.
    """
    if not usage:
        return None

    # Only count non-cached tokens: input + output
    # Exclude cache_read_input_tokens (seems to include shared system cache)
    # Exclude cache_creation_input_tokens (represents what's being cached, not consumed)
    total_tokens = (
        usage.get("input_tokens", 0) +
        usage.get("output_tokens", 0)
    )

    if total_tokens == 0:
        return None

    # Get context window for model
    context_window = MODEL_CONTEXT_WINDOWS.get(model, MODEL_CONTEXT_WINDOWS["default"])

    # Calculate remaining percentage
    used_percent = (total_tokens / context_window) * 100
    remaining_percent = 100 - used_percent

    return max(0, remaining_percent)


async def start_session(chat_id: int, thread_id: int, folder_name: str, bot: Bot) -> bool:
    """Start a new Claude session for a Telegram thread."""
    cwd = PROJECTS_DIR / folder_name
    if not cwd.exists():
        return False

    return await _start_session_impl(chat_id, thread_id, str(cwd), folder_name, bot)


async def start_session_local(chat_id: int, thread_id: int, cwd: str, bot: Bot) -> bool:
    """Start a Claude session with an absolute path (for local project bot).

    Logs are stored in <cwd>/.bot-logs/ instead of global logs dir.
    """
    cwd_path = Path(cwd)
    if not cwd_path.exists():
        return False

    # Use project-local logs directory
    logs_dir = cwd_path / ".bot-logs"
    return await _start_session_impl(chat_id, thread_id, cwd, cwd_path.name, bot, logs_dir)


async def _start_session_impl(
    chat_id: int,
    thread_id: int,
    cwd: str,
    display_name: str,
    bot: Bot,
    logs_dir: Optional[Path] = None
) -> bool:
    """Internal: start Telegram session with given cwd path."""
    # Create logger (use custom logs_dir if provided)
    logger = SessionLogger(thread_id, chat_id, cwd, logs_dir)

    # Load contextual commands from project's commands/ directory
    contextual_commands = load_contextual_commands(cwd)

    # Create platform client and formatter
    platform = TelegramClient(
        bot=bot,
        chat_id=chat_id,
        thread_id=thread_id,
        logger=logger,
    )
    formatter = TelegramFormatter()

    # Store session with platform abstraction
    sessions[thread_id] = ClaudeSession(
        chat_id=chat_id,
        thread_id=thread_id,
        cwd=cwd,
        platform=platform,
        formatter=formatter,
        bot=bot,  # Keep for backwards compat
        logger=logger,
        contextual_commands=contextual_commands,
    )

    # Register commands with Telegram for autocompletion
    await register_commands_for_chat(bot, chat_id, contextual_commands)

    # Send welcome message using platform
    await platform.send_message(f"Claude session started in {formatter.code(display_name)}")

    return True


async def start_session_ambient(chat_id: int, thread_id: int, bot: Bot) -> bool:
    """Start an ambient Claude session with home folder as cwd.

    Used for #general or other permanent ambient sessions that don't
    need a specific project folder.

    Args:
        chat_id: Telegram chat ID
        thread_id: Telegram thread ID (e.g., GENERAL_TOPIC_ID)
        bot: Telegram Bot instance

    Returns:
        True if session started successfully
    """
    home_dir = str(Path.home())
    return await _start_session_impl(chat_id, thread_id, home_dir, "~", bot)


async def start_session_ambient_discord(channel_id: int, channel: Any) -> bool:
    """Start an ambient Claude session for Discord with home folder as cwd.

    Used for #general or other ambient channels that don't need a specific project.

    Args:
        channel_id: Discord channel ID (used as session key)
        channel: Discord channel object (TextChannel, Thread, or similar)

    Returns:
        True if session started successfully
    """
    home_dir = str(Path.home())
    return await start_session_discord(channel_id, home_dir, channel, display_name="~")


async def start_session_discord(channel_id: int, project_path: str, channel: Any, display_name: Optional[str] = None) -> bool:
    """Start a new Claude session for a Discord channel.

    Args:
        channel_id: Discord channel ID (used as session key)
        project_path: Absolute path to project directory
        channel: Discord channel object (TextChannel or similar)
        display_name: Optional display name (defaults to folder name)

    Returns:
        True if session started successfully
    """
    if not DISCORD_AVAILABLE:
        _log.error("Discord support not available - discord.py not installed")
        return False

    cwd_path = Path(project_path)
    if not cwd_path.exists():
        return False

    display_name = display_name or cwd_path.name

    # Create logger - use project-local logs
    logs_dir = cwd_path / ".bot-logs"
    logger = SessionLogger(channel_id, 0, str(cwd_path), logs_dir)  # chat_id=0 for Discord

    # Load contextual commands
    contextual_commands = load_contextual_commands(str(cwd_path))

    # Create Discord platform client and formatter
    # Import here to avoid issues when discord.py not installed
    from platforms.discord import DiscordClient as DC, DiscordFormatter as DF
    platform = DC(channel=channel, logger=logger)
    formatter = DF()

    # Store session (keyed by channel_id for Discord)
    sessions[channel_id] = ClaudeSession(
        chat_id=0,  # Not used for Discord
        thread_id=channel_id,  # Use channel_id as session key
        cwd=str(cwd_path),
        platform=platform,
        formatter=formatter,
        bot=None,  # No Telegram bot
        logger=logger,
        contextual_commands=contextual_commands,
    )

    # Send welcome message
    await platform.send_message(f"Claude session started in `{display_name}`")

    return True


async def stop_session(thread_id: int) -> bool:
    """Stop and clean up a Claude session."""
    session = sessions.get(thread_id)
    if not session:
        return False

    session.active = False

    # Close logger
    if session.logger:
        session.logger.log_session_end("stopped")
        session.logger.close()

    del sessions[thread_id]
    return True


# Patterns that indicate recoverable API errors requiring session reset
_RECOVERABLE_ERROR_PATTERNS = [
    "image dimensions exceed max allowed size",
    "image.source.base64.data",
    "many-image requests",
]


async def _handle_recoverable_api_error(session: ClaudeSession, error_str: str) -> bool:
    """Handle recoverable API errors by resetting session state.

    Some API errors (like image dimension limits) poison the conversation history.
    When resumed, they keep failing. This function detects such errors and resets
    the session_id so the next request starts fresh.

    Args:
        session: The Claude session
        error_str: The full error string (exception + stderr)

    Returns:
        True if the error was handled (caller should return), False otherwise
    """
    error_lower = error_str.lower()

    # Check if this is an image-related recoverable error
    is_image_error = any(pattern.lower() in error_lower for pattern in _RECOVERABLE_ERROR_PATTERNS)

    if not is_image_error:
        return False

    # Log the recovery action
    if session.logger:
        session.logger._write_log(f"RECOVERABLE ERROR detected, resetting session: {error_str[:200]}")

    # Reset session_id to start fresh conversation
    old_session_id = session.session_id
    session.session_id = None

    platform = session.get_platform()
    if platform:
        formatter = session.get_formatter()
        await platform.send_message(
            f"⚠️ {formatter.bold('Session Reset')}\n\n"
            f"An image in the conversation exceeded API size limits. "
            f"Session history has been cleared.\n\n"
            f"Please re-send your last message to continue."
        )

    if session.logger:
        session.logger._write_log(f"Session reset: {old_session_id} -> None")

    return True


async def send_to_claude(thread_id: int, prompt: str, bot: Optional[Bot] = None) -> None:
    """Send a message to Claude and stream the response.

    Uses platform abstraction for cross-platform support.
    Bot parameter is deprecated and ignored.
    """
    session = sessions.get(thread_id)
    if not session or not session.active:
        return

    platform = session.get_platform()
    if not platform:
        return

    # Log user input
    if session.logger:
        session.logger.log_user_input(prompt)

    # Send typing indicator
    await send_typing_action(session)

    # Track current response message for streaming edits
    response_ref: Optional[MessageRef] = None
    response_text = ""
    response_msg_text_len = 0  # Length of text in current response_ref

    # Buffer for batching consecutive tool calls of same type
    tool_buffer: list[tuple[str, dict]] = []  # [(tool_name, input), ...]
    tool_buffer_name: Optional[str] = None  # Current tool type being buffered
    tool_buffer_ref: Optional[MessageRef] = None  # Message being edited for batch

    # Buffer for diff images to send as media group at end
    diff_images: list[tuple[BytesIO, str]] = []  # [(image_buffer, filename), ...]

    # Track model and usage for context calculation
    current_model: Optional[str] = None
    last_usage: Optional[dict] = None

    def format_current_tool_buffer() -> str:
        """Format current tool buffer contents using session formatter."""
        formatter = session.get_formatter()
        if len(tool_buffer) == 1:
            name, input_dict = tool_buffer[0]
            return formatter.format_tool_call(name, input_dict)
        else:
            return formatter.format_tool_calls_batch(tool_buffer_name or "Tool", tool_buffer)

    async def update_tool_buffer_message():
        """Send or edit the tool buffer message."""
        nonlocal tool_buffer_ref
        text = format_current_tool_buffer()

        if tool_buffer_ref and tool_buffer_ref.platform_data:
            # Edit existing message
            try:
                await platform.edit_message(tool_buffer_ref, text)
            except Exception as e:
                if session.logger and "message is not modified" not in str(e).lower():
                    session.logger.log_error("update_tool_buffer_message", e)
        else:
            # Send new message
            tool_buffer_ref = await send_message(session, text=text)

    async def flush_tool_buffer():
        """Clear tool buffer state (message already sent/edited)."""
        nonlocal tool_buffer, tool_buffer_name, tool_buffer_ref
        tool_buffer = []
        tool_buffer_name = None
        tool_buffer_ref = None

    try:
        # Check if AGENTS.md exists and pre-load its content into system prompt
        agents_md_path = Path(session.cwd) / "AGENTS.md"
        system_prompt: Optional[SystemPromptPreset] = None
        if agents_md_path.exists():
            try:
                agents_content = agents_md_path.read_text()
                system_prompt = SystemPromptPreset(
                    type="preset",
                    preset="claude_code",
                    append=f"# Project Context (from AGENTS.md)\n\n{agents_content}"
                )
            except Exception:
                # If we can't read the file, fall back to instruction-based approach
                system_prompt = SystemPromptPreset(
                    type="preset",
                    preset="claude_code",
                    append="IMPORTANT: This project has an AGENTS.md file in the root directory. "
                           "Read it at the start of the session to understand project context and instructions."
                )

        # Create MCP servers bound to this session
        telegram_mcp = create_telegram_mcp_server(session)

        # Configure options - use permission handler for interactive tool approval
        options = ClaudeAgentOptions(
            allowed_tools=[],  # Empty - let can_use_tool handle all permissions
            can_use_tool=create_permission_handler(session),
            permission_mode="acceptEdits",
            cwd=session.cwd,
            resume=session.session_id,  # Resume previous conversation if exists
            system_prompt=system_prompt,
            setting_sources=["user", "project"],  # Load skills from ~/.claude/skills/ and .claude/skills/
            max_thinking_tokens=10000,  # Enable interleaved thinking between tool calls
            mcp_servers={
                "telegram-tools": telegram_mcp,
            },
            hooks={
                "PreCompact": [
                    HookMatcher(hooks=[create_pre_compact_hook(session)])
                ]
            }
        )

        # Query Claude using ClaudeSDKClient (required for can_use_tool support)
        # can_use_tool requires streaming mode - wrap prompt in async generator
        # Format from SDK examples/streaming_mode.py
        async def prompt_stream():
            yield {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": prompt
                },
                "parent_tool_use_id": None,
                "session_id": session.session_id or "default"
            }

        async with ClaudeSDKClient(options=options) as client:
            # Store client reference for interrupt support
            session.client = client

            await client.query(prompt_stream())
            async for message in client.receive_response():
                # Refresh typing indicator on each message
                await send_typing_action(session)

                # Log SDK message
                if session.logger:
                    session.logger.log_sdk_message(message)

                # Handle different message types based on their class
                if isinstance(message, (UserMessage, AssistantMessage)):
                    content = message.content
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, TextBlock):
                                # Text content - flush tools first, then accumulate text
                                await flush_tool_buffer()

                                response_text += block.text
                                response_ref, response_msg_text_len = await send_or_edit_response(
                                    session, existing_ref=response_ref, text=response_text, msg_text_len=response_msg_text_len
                                )

                            elif isinstance(block, ToolUseBlock):
                                # Tool use block - buffer it
                                if response_text.strip():
                                    response_ref, response_msg_text_len = await send_or_edit_response(
                                        session, existing_ref=response_ref, text=response_text, msg_text_len=response_msg_text_len
                                    )
                                    response_ref = None
                                    response_text = ""
                                    response_msg_text_len = 0

                                tool_name = block.name
                                tool_input = block.input

                                # If different tool type, flush buffer first
                                if tool_buffer_name and tool_buffer_name != tool_name:
                                    await flush_tool_buffer()

                                # Add to buffer and update message immediately
                                tool_buffer.append((tool_name, tool_input))
                                tool_buffer_name = tool_name
                                await update_tool_buffer_message()

                                # Buffer diff image for Edit tool
                                if tool_name == "Edit" and "old_string" in tool_input and "new_string" in tool_input:
                                    file_path = tool_input.get("file_path", "file")
                                    img_buffer = edit_to_image(
                                        file_path=file_path,
                                        old_string=tool_input["old_string"],
                                        new_string=tool_input["new_string"]
                                    )
                                    if img_buffer:
                                        filename = file_path.split("/")[-1] if "/" in file_path else file_path
                                        diff_images.append((img_buffer, filename))

                            elif isinstance(block, ThinkingBlock):
                                # Interleaved thinking - flush any pending content first
                                await flush_tool_buffer()
                                if response_text.strip():
                                    response_ref, response_msg_text_len = await send_or_edit_response(
                                        session, existing_ref=response_ref, text=response_text, msg_text_len=response_msg_text_len
                                    )
                                    response_ref = None
                                    response_text = ""
                                    response_msg_text_len = 0

                                # Send thinking content via platform (handles formatting)
                                thinking_text = block.thinking
                                if thinking_text:
                                    await platform.send_thinking(thinking_text)

                    elif isinstance(content, str) and content:
                        # Tool result - flush tool buffer first
                        await flush_tool_buffer()

                        output = format_tool_output(content)
                        if output:
                            # Escape HTML entities in output
                            formatter = session.get_formatter()
                            safe_output = formatter.escape_text(output)
                            await send_message(session, text=formatter.code_block(safe_output))

                        # Refresh typing - more content likely coming after tool result
                        await send_typing_action(session)

                # Capture model from AssistantMessage
                if isinstance(message, AssistantMessage):
                    current_model = message.model

                # Capture session_id and usage from ResultMessage
                if isinstance(message, ResultMessage):
                    if message.session_id:
                        session.session_id = message.session_id
                    if message.usage:
                        last_usage = message.usage
                    # Log stats (but don't show cost to user - it's included in subscription)
                    cost = message.total_cost_usd
                    duration = message.duration_ms
                    if session.logger and cost is not None:
                        session.logger.log_session_stats(cost, duration, last_usage or {})

            # Flush any remaining buffers (inside async with, after loop)
            await flush_tool_buffer()
            if response_text.strip() and response_ref is None:
                await send_message(session, text=response_text)

            # Send diff images as media group (gallery)
            if diff_images:
                await send_diff_images_gallery(session, images=diff_images)

            # Calculate and store context remaining
            if last_usage:
                context_remaining = calculate_context_remaining(last_usage, current_model or "default")
                if context_remaining is not None:
                    session.last_context_percent = context_remaining

                    # Warn user if context is running low - append to last text response
                    if context_remaining < CONTEXT_WARNING_THRESHOLD:
                        if session.logger:
                            session.logger._write_log(f"CONTEXT WARNING: {context_remaining:.1f}% remaining")

                        warning = f"\n\n⚠️ {context_remaining:.0f}% context remaining"
                        max_len = platform.max_message_length

                        # Only append to text response message (not tool messages)
                        if response_ref and response_ref.platform_data and response_msg_text_len > 0:
                            # Check if warning fits in current message
                            if response_msg_text_len + len(warning) <= max_len:
                                # Get the text currently in the message and append warning
                                current_msg_text = response_text[-response_msg_text_len:] if len(response_text) > response_msg_text_len else response_text
                                warning_text = current_msg_text + warning
                                try:
                                    await platform.edit_message(response_ref, warning_text)
                                except Exception:
                                    # Edit failed, send as separate message
                                    await send_message(session, text=f"⚠️ {context_remaining:.0f}% context remaining")
                            else:
                                # Warning doesn't fit, send separately
                                await send_message(session, text=f"⚠️ {context_remaining:.0f}% context remaining")

            # Clear client reference when done
            session.client = None

    except ProcessError as e:
        # Log stderr from CLI process
        if session.logger:
            session.logger.log_error("send_to_claude", e)
            if e.stderr:
                session.logger.log_stderr(e.stderr)

        # Check for recoverable API errors (e.g., oversized images poisoning session)
        error_str = str(e) + (e.stderr or "")
        if await _handle_recoverable_api_error(session, error_str):
            return  # Error was handled and user notified

        error_msg = f"❌ Error: {str(e)}"
        if e.stderr:
            error_msg += f"\nStderr: {e.stderr[:500]}"
        await send_message(session, text=error_msg)
    except Exception as e:
        error_str = str(e)
        if session.logger:
            session.logger.log_error("send_to_claude", e)

        # Check for recoverable API errors (e.g., oversized images poisoning session)
        if await _handle_recoverable_api_error(session, error_str):
            return  # Error was handled and user notified

        error_msg = f"❌ Error: {error_str}"
        await send_message(session, text=error_msg)
    finally:
        # Always clear client reference
        session.client = None


async def send_or_edit_response(
    session: ClaudeSession,
    bot: Optional[Bot] = None,
    existing_ref: Optional[MessageRef] = None,
    text: str = "",
    msg_text_len: int = 0
) -> tuple[Optional[MessageRef], int]:
    """Send a new response message or edit an existing one.

    Handles overflow by starting new messages when text exceeds platform limit.
    Splits long responses into multiple messages to avoid truncation.

    Args:
        session: The Claude session
        bot: Deprecated, ignored - uses session.get_platform()
        existing_ref: Existing message ref to edit, or None to send new
        text: Full accumulated text to display
        msg_text_len: Length of text already in existing_ref (for overflow detection)

    Returns:
        Tuple of (last message ref, length of text in that message)
    """
    if not text.strip():
        return existing_ref, msg_text_len

    platform = session.get_platform()
    max_len = platform.max_message_length if platform else 4000

    # Check if we need to overflow to new messages
    if len(text) > max_len and existing_ref and msg_text_len > 0:
        # Current message is full, send overflow text as new message(s)
        overflow_text = text[msg_text_len:]

        # Split overflow into chunks and send each as a new message
        chunks = split_text(overflow_text, max_len)
        last_ref: Optional[MessageRef] = None
        last_len = 0

        for chunk in chunks:
            new_ref = await _send_with_fallback(session, text=chunk, existing_ref=None)
            if new_ref:
                last_ref = new_ref
                last_len = len(chunk)

        return last_ref if last_ref else existing_ref, last_len if last_ref else msg_text_len

    # For edits, truncate if too long (can't split an edit into multiple messages)
    # For new messages, send_message will handle splitting via split_text
    display_text = text
    if existing_ref and existing_ref.platform_data and len(display_text) > max_len:
        display_text = display_text[:max_len - 10] + "\n..."

    result_ref = await _send_with_fallback(session, text=display_text, existing_ref=existing_ref)
    if result_ref:
        return result_ref, len(display_text) if len(display_text) <= max_len else max_len
    return existing_ref, msg_text_len


async def _send_with_fallback(
    session: ClaudeSession,
    bot: Optional[Bot] = None,
    text: str = "",
    existing_ref: Optional[MessageRef] = None,
    max_retries: int = 3
) -> Optional[MessageRef]:
    """Send or edit a message using platform abstraction.

    Platform client handles retries and fallbacks internally.

    Args:
        session: The Claude session
        bot: Deprecated, ignored - uses session.get_platform()
        text: Text to send (markdown format, will be converted)
        existing_ref: Existing message ref to edit, or None to send new
        max_retries: Retry attempts (handled by platform)

    Returns:
        MessageRef if successful, None otherwise
    """
    platform = session.get_platform()
    if not platform:
        return None

    try:
        if existing_ref and existing_ref.platform_data:
            await platform.edit_message(existing_ref, text)
            return existing_ref
        else:
            return await platform.send_message(text)
    except Exception as e:
        error_str = str(e).lower()
        # "message is not modified" is not an error
        if "message is not modified" in error_str:
            return existing_ref
        if session.logger:
            session.logger.log_error("_send_with_fallback", e)
        return None


async def send_message(
    session: ClaudeSession,
    bot: Optional[Bot] = None,
    text: str = "",
    parse_mode: Optional[str] = None
) -> Optional[MessageRef]:
    """Send a new message using platform abstraction.

    Args:
        session: The Claude session
        bot: Deprecated, ignored - uses session.get_platform()
        text: Text to send (will be converted to platform format)
        parse_mode: Deprecated, ignored - platform handles formatting

    Returns:
        MessageRef for later editing, or None if send failed
    """
    if not text.strip():
        return None

    platform = session.get_platform()
    if not platform:
        return None

    # Platform client handles rate limiting internally
    return await platform.send_message(text)


async def send_typing_action(session: ClaudeSession, bot: Optional[Bot] = None) -> None:
    """Send typing indicator if enough time has passed.

    Uses platform abstraction. Bot parameter is deprecated and ignored.
    """
    now = time.time()
    if now - session.last_typing_action < TYPING_ACTION_INTERVAL:
        return

    platform = session.get_platform()
    if platform:
        try:
            await platform.send_typing()
            session.last_typing_action = now
        except Exception as e:
            if session.logger and "flood" not in str(e).lower():
                session.logger.log_debug("send_typing_action", f"Failed: {e}")


async def send_diff_images_gallery(
    session: ClaudeSession,
    bot: Optional[Bot] = None,
    images: Optional[list[tuple[BytesIO, str]]] = None
) -> None:
    """Send diff images as a media group (gallery).

    Uses platform abstraction. Bot parameter is deprecated and ignored.
    """
    if not images:
        return

    platform = session.get_platform()
    if not platform:
        return

    try:
        # Convert to format expected by platform.send_photos
        # Platform expects (BytesIO, caption) tuples
        formatted_images = [(img_buffer, f"📝 {filename}") for img_buffer, filename in images]
        await platform.send_photos(formatted_images)
    except Exception as e:
        if session.logger:
            session.logger.log_error("send_diff_images_gallery", e)


def escape_html(text: str) -> str:
    """Escape HTML entities for Telegram."""
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def strip_html_tags(text: str) -> str:
    """Remove all HTML tags from text, leaving only content."""
    return re.sub(r'<[^>]+>', '', text)


class TelegramHTMLRenderer(mistune.HTMLRenderer):
    """Custom mistune renderer that outputs Telegram-compatible HTML.

    Telegram only supports a subset of HTML tags:
    <b>, <strong>, <i>, <em>, <u>, <ins>, <s>, <strike>, <del>,
    <span class="tg-spoiler">, <a href="">, <code>, <pre>
    """

    def text(self, text: str) -> str:
        """Escape HTML entities in plain text."""
        return escape_html(text)

    def emphasis(self, text: str) -> str:
        """Render *italic* text."""
        return f'<i>{text}</i>'

    def strong(self, text: str) -> str:
        """Render **bold** text."""
        return f'<b>{text}</b>'

    def codespan(self, text: str) -> str:
        """Render `inline code`."""
        return f'<code>{escape_html(text)}</code>'

    def block_code(self, code: str, info: Optional[str] = None) -> str:
        """Render ```code blocks```."""
        return f'<pre>{escape_html(code)}</pre>\n'

    def link(self, text: str, url: str, title: Optional[str] = None) -> str:
        """Render [text](url) links."""
        return f'<a href="{escape_html(url)}">{text}</a>'

    def strikethrough(self, text: str) -> str:
        """Render ~~strikethrough~~ text."""
        return f'<s>{text}</s>'

    def heading(self, text: str, level: int, **attrs: Any) -> str:
        """Render headings as bold text (Telegram doesn't support h1-h6)."""
        prefix = '#' * level
        return f'<b>{prefix} {text}</b>\n'

    def paragraph(self, text: str) -> str:
        """Render paragraphs with newlines."""
        return f'{text}\n\n'

    def linebreak(self) -> str:
        """Render line breaks."""
        return '\n'

    def softbreak(self) -> str:
        """Render soft breaks (single newlines in source)."""
        return '\n'

    def blank_line(self) -> str:
        """Render blank lines."""
        return '\n'

    def thematic_break(self) -> str:
        """Render horizontal rules as dashes."""
        return '\n---\n'

    def block_quote(self, text: str) -> str:
        """Render blockquotes with > prefix."""
        # Add > prefix to each line
        lines = text.strip().split('\n')
        quoted = '\n'.join(f'> {line}' for line in lines)
        return f'{quoted}\n\n'

    def list(self, text: str, ordered: bool, **attrs: Any) -> str:
        """Render lists."""
        return f'{text}\n'

    def list_item(self, text: str) -> str:
        """Render list items."""
        return f'• {text.strip()}\n'

    def image(self, text: str, url: str, title: Optional[str] = None) -> str:
        """Render images as links (Telegram doesn't support inline images in text)."""
        return f'[{text}]({escape_html(url)})'

    # Table rendering - Telegram doesn't support HTML tables, so render as plain text
    def table(self, text: str) -> str:
        """Render table as plain text."""
        return f'{text}\n'

    def table_head(self, text: str) -> str:
        """Render table header."""
        return f'{text}'

    def table_body(self, text: str) -> str:
        """Render table body."""
        return text

    def table_row(self, text: str) -> str:
        """Render table row as pipe-separated values."""
        return f'{text}|\n'

    def table_cell(self, text: str, align: Optional[str] = None, head: bool = False) -> str:
        """Render table cell."""
        if head:
            return f'| <b>{text}</b> '
        return f'| {text} '


# Create a global markdown parser instance with the Telegram renderer
# Enable strikethrough plugin for ~~text~~ support
_telegram_md = mistune.create_markdown(
    renderer=TelegramHTMLRenderer(),
    plugins=['strikethrough', 'table']
)


def markdown_to_html(text: str) -> str:
    """Convert markdown to Telegram-compatible HTML using mistune.

    This properly handles all markdown edge cases including:
    - Nested formatting
    - Tables with special characters
    - Code blocks containing markdown-like syntax
    - Complex inline code
    """
    try:
        result = _telegram_md(text)
        # mistune can return str or tuple, ensure we have str
        if isinstance(result, tuple):
            result = result[0]
        # Type assertion for mypy - at this point result is always str
        result_str: str = str(result) if not isinstance(result, str) else result
        # Clean up excessive newlines
        result_str = re.sub(r'\n{3,}', '\n\n', result_str)
        return result_str.strip()
    except Exception:
        # If parsing fails, return escaped plain text
        return escape_html(text)


def format_tool_call(name: str, input_dict: dict) -> str:
    """Format a tool call for display in Telegram (HTML)."""
    # Show key args, truncate long values
    parts = []
    for k, v in input_dict.items():
        v_str = str(v)
        if len(v_str) > 50:
            v_str = v_str[:50] + "..."
        # Escape HTML in values
        v_str = escape_html(v_str)
        parts.append(f"{k}={v_str}")

    args_str = ", ".join(parts) if parts else ""
    return f"🔧 <b>{name}</b>({args_str})"


def format_tool_calls_batch(tool_name: str, calls: list[tuple[str, dict]]) -> str:
    """Format multiple tool calls of same type as a single message (HTML)."""
    # Extract the key argument for each call (usually file_path, pattern, command, etc.)
    items = []
    for name, input_dict in calls:
        # Try to get the most relevant argument
        key_arg = None
        for key in ['file_path', 'path', 'pattern', 'command', 'query', 'prompt', 'url']:
            if key in input_dict:
                key_arg = str(input_dict[key])
                break

        if key_arg is None and input_dict:
            # Use first argument value
            key_arg = str(list(input_dict.values())[0])

        if key_arg:
            # Truncate long values
            if len(key_arg) > 60:
                key_arg = key_arg[:57] + "..."
            # Escape HTML in values
            key_arg = escape_html(key_arg)
            items.append(f"  • {key_arg}")
        else:
            items.append(f"  • (no args)")

    return f"🔧 <b>{tool_name}</b> ({len(calls)} calls)\n" + "\n".join(items)


def format_tool_output(content: Any) -> str:
    """Format tool output for display, truncating if needed."""
    if content is None:
        return ""

    text = str(content)
    if len(text) > 1000:
        return text[:1000] + "\n... (truncated)"
    return text


def split_text(text: str, max_len: int = 4000) -> list[str]:
    """Split text into chunks suitable for Telegram messages."""
    if len(text) <= max_len:
        return [text]

    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break

        # Try to split at newline
        split_pos = text.rfind('\n', 0, max_len)
        if split_pos == -1 or split_pos < max_len // 2:
            # Try space
            split_pos = text.rfind(' ', 0, max_len)
        if split_pos == -1 or split_pos < max_len // 2:
            # Hard split
            split_pos = max_len

        chunks.append(text[:split_pos])
        text = text[split_pos:].lstrip()

    return chunks


