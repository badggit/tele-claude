from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional, Any

from core.types import ReplyTarget, SessionStats, Trigger
from platforms.protocol import TextMessage

_log = logging.getLogger("tele-claude.session_actor")


@dataclass
class SessionActor:
    """Isolated session handling one conversation."""

    session_key: str
    platform: str
    cwd: str
    reply_target: ReplyTarget
    claude_session: Any

    _mailbox: asyncio.Queue[Trigger] = field(default_factory=asyncio.Queue)
    _run_loop_task: Optional[asyncio.Task] = None
    _generation_id: int = 0

    active: bool = True
    current_task: Optional[asyncio.Task] = None
    pending_permission: Optional[asyncio.Future] = None
    stats: SessionStats = field(default_factory=SessionStats)

    async def start(self) -> None:
        """Start the actor's run loop."""
        if self._run_loop_task is None or self._run_loop_task.done():
            self._run_loop_task = asyncio.create_task(self._run_loop())

    async def enqueue(self, trigger: Trigger) -> None:
        """Add trigger to mailbox."""
        await self._mailbox.put(trigger)

    async def _run_loop(self) -> None:
        """Main actor loop - processes mailbox sequentially."""
        while self.active:
            try:
                trigger = await self._mailbox.get()
            except asyncio.CancelledError:
                break

            try:
                if self.current_task and not self.current_task.done():
                    self._generation_id += 1
                    self.stats.interrupt_count += 1
                    await self._cancel_current_task()

                self._generation_id += 1
                await self._handle_prompt(trigger.prompt, trigger.images, self._generation_id)
            except Exception:
                self.stats.error_count += 1
                _log.exception("SessionActor run loop failed session_key=%s", self.session_key)
            finally:
                self._mailbox.task_done()

    async def _handle_prompt(self, prompt: str, images: list[str], gen_id: int) -> None:
        """Process a user prompt. gen_id guards stale operations."""
        if not prompt.strip() and not images:
            return

        # Handle image-only messages by buffering until a prompt arrives.
        if images and not prompt.strip():
            if hasattr(self.claude_session, "pending_image_path"):
                self.claude_session.pending_image_path = images[0]
            return

        if hasattr(self.claude_session, "pending_image_path"):
            pending_image = self.claude_session.pending_image_path
            if pending_image:
                self.claude_session.pending_image_path = None
                prompt = f"{pending_image}\n\n{prompt}" if prompt.strip() else pending_image

        if images:
            image_block = "\n".join(images)
            prompt = f"{image_block}\n\n{prompt}" if prompt.strip() else image_block

        if prompt.startswith("/"):
            from commands import get_command_prompt

            command_name = prompt.split()[0].lstrip("/").split("@")[0]
            contextual = getattr(self.claude_session, "contextual_commands", [])
            command_prompt = get_command_prompt(command_name, contextual)
            if command_prompt is not None:
                if command_prompt == "":
                    # Allow actor-side fallback for platform-owned commands when
                    # listener path does not implement them (e.g. Discord /model).
                    if await self._handle_platform_command(command_name, prompt):
                        return
                    return
                prompt = command_prompt

        self.stats.message_count += 1
        self.stats.last_activity = time.time()

        try:
            import session as session_module

            thread_id = getattr(self.claude_session, "thread_id", None)
            bot = getattr(self.claude_session, "bot", None)
            if thread_id is None:
                _log.warning("Missing thread_id for session_key=%s", self.session_key)
                return
            task = session_module.start_claude_task(thread_id, prompt, bot)
            if task is None:
                self.stats.error_count += 1
                _log.warning("Failed to start Claude task session_key=%s", self.session_key)
                return
            self.current_task = task
        except Exception:
            self.stats.error_count += 1
            _log.exception("Failed to start Claude task session_key=%s", self.session_key)

    async def _handle_platform_command(self, command_name: str, prompt: str) -> bool:
        """Handle platform-owned commands that need actor-side fallback."""
        if command_name == "model":
            await self._handle_model_command(prompt)
            return True
        return False

    async def _handle_model_command(self, prompt: str) -> None:
        """Handle /model command for sessions routed through SessionActor."""
        from config import AVAILABLE_MODELS, CLAUDE_MODEL

        args = prompt.split(maxsplit=1)
        current_model = getattr(self.claude_session, "model_override", None) or CLAUDE_MODEL or "default"

        if len(args) < 2:
            models = "\n".join(f"  `{name}`" for name in AVAILABLE_MODELS)
            await self.reply_target.send(
                TextMessage(
                    f"Current model: `{current_model}`\n\n"
                    f"Available models:\n{models}\n\n"
                    "Usage: /model <name>"
                )
            )
            return

        model_name = args[1].strip()
        if model_name not in AVAILABLE_MODELS:
            models = "\n".join(f"  `{name}`" for name in AVAILABLE_MODELS)
            await self.reply_target.send(
                TextMessage(
                    f"Unknown model: `{model_name}`\n\n"
                    f"Available models:\n{models}"
                )
            )
            return

        setattr(self.claude_session, "model_override", model_name)
        await self.reply_target.send(
            TextMessage(f"Model switched to `{model_name}` for this session.")
        )

    async def _cancel_current_task(self) -> None:
        """Cancel current task and wait for cleanup."""
        if self.current_task:
            interrupted = False
            try:
                import session as session_module

                thread_id = getattr(self.claude_session, "thread_id", None)
                if thread_id is not None:
                    interrupted = await session_module.interrupt_session(thread_id)
            except Exception:
                _log.exception("interrupt_session failed session_key=%s", self.session_key)

            if not interrupted and self.current_task and not self.current_task.done():
                self.current_task.cancel()
                try:
                    await self.current_task
                except asyncio.CancelledError:
                    pass
            self.current_task = None

        if self.pending_permission and not self.pending_permission.done():
            self.pending_permission.cancel()
        self.pending_permission = None

    async def resolve_permission(self, allowed: bool, always: bool) -> None:
        """Resolve a pending permission request."""
        if self.pending_permission and not self.pending_permission.done():
            self.pending_permission.set_result(allowed)
        self.pending_permission = None

    async def close(self) -> None:
        """Clean up resources."""
        self.active = False
        await self._cancel_current_task()
        if self._run_loop_task and not self._run_loop_task.done():
            self._run_loop_task.cancel()
            try:
                await self._run_loop_task
            except asyncio.CancelledError:
                pass
