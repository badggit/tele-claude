from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional, Protocol, runtime_checkable, Any

from core.session_actor import SessionActor
from core.session_store import session_store
from core.types import ReplyTarget, Trigger

_log = logging.getLogger("tele-claude.dispatcher.v2")


@runtime_checkable
class TransportListener(Protocol):
    """Interface for transport-specific message listeners."""

    platform: str

    async def start(self, on_trigger: Callable[[Trigger], Awaitable[None]]) -> None:
        """Start listening. Call on_trigger for each incoming event."""
        ...

    async def stop(self) -> None:
        """Stop listening."""
        ...

    def create_reply_target(self, reply_context: dict[str, Any]) -> ReplyTarget:
        """Create a ReplyTarget for responding to this trigger."""
        ...

    async def create_session(self, trigger: Trigger, cwd: str) -> Any:
        """Create and return a Claude session for this trigger."""
        ...

    def resolve_cwd(self, trigger: Trigger) -> Optional[str]:
        """Resolve cwd for this trigger, if possible."""
        ...


class Dispatcher:
    """Central coordinator for all transports and sessions."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionActor] = {}
        self._listeners: dict[str, TransportListener] = {}
        self._session_lock = asyncio.Lock()
        session_store.load()
        session_store.cleanup_expired()

    @property
    def sessions(self) -> dict[str, SessionActor]:
        return self._sessions

    def add_listener(self, listener: TransportListener) -> None:
        if listener.platform in self._listeners:
            raise ValueError(f"Listener already registered: {listener.platform}")
        self._listeners[listener.platform] = listener

    def get_listener(self, platform: str) -> Optional[TransportListener]:
        return self._listeners.get(platform)

    async def start(self) -> None:
        for listener in self._listeners.values():
            await listener.start(self.route_trigger)

    async def stop(self) -> None:
        for session in list(self._sessions.values()):
            await session.close()
        for listener in self._listeners.values():
            await listener.stop()

    async def route_trigger(self, trigger: Trigger) -> None:
        """Route trigger to session. Enqueues and returns immediately."""
        async with self._session_lock:
            if trigger.session_key not in self._sessions:
                try:
                    session = await self._create_session(trigger)
                except Exception:
                    _log.exception("Failed to create session for key=%s", trigger.session_key)
                    return
                if session is None:
                    return
                self._sessions[trigger.session_key] = session
                await session.start()

        session = self._sessions.get(trigger.session_key)
        if session is None:
            return
        await session.enqueue(trigger)

    async def _create_session(self, trigger: Trigger) -> Optional[SessionActor]:
        listener = self._listeners.get(trigger.platform)
        if not listener:
            raise ValueError(f"No listener for platform: {trigger.platform}")

        cwd = self._resolve_cwd(listener, trigger)
        if not cwd:
            _log.warning("No cwd resolved for session_key=%s", trigger.session_key)
            return None

        reply_target = listener.create_reply_target(trigger.reply_context)
        claude_session = await listener.create_session(trigger, cwd)
        persisted = session_store.get(trigger.session_key)
        if persisted:
            if not persisted.cwd:
                _log.warning("Persisted session missing cwd for %s", trigger.session_key)
            elif hasattr(claude_session, "session_id"):
                claude_session.session_id = persisted.claude_session_id
                _log.info("Restored session_id for %s", trigger.session_key)
        return SessionActor(
            session_key=trigger.session_key,
            platform=trigger.platform,
            cwd=cwd,
            reply_target=reply_target,
            claude_session=claude_session,
        )

    def _resolve_cwd(self, listener: TransportListener, trigger: Trigger) -> Optional[str]:
        cwd = trigger.reply_context.get("cwd")
        if cwd:
            return cwd
        if hasattr(listener, "resolve_cwd"):
            try:
                return listener.resolve_cwd(trigger)
            except Exception:
                _log.exception("resolve_cwd failed platform=%s", listener.platform)
        return None
