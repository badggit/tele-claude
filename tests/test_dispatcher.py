"""Tests for core/dispatcher.py."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

import pytest

PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.dispatcher import Dispatcher
from core.types import Trigger, make_session_key


class TestResolveCwd:
    """Tests for Dispatcher._resolve_cwd."""

    def test_uses_cwd_from_reply_context(self):
        """If reply_context has cwd, use it."""
        dispatcher = Dispatcher()
        listener = MagicMock()
        trigger = Trigger(
            platform="discord",
            session_key="discord:123",
            prompt="test",
            reply_context={"cwd": "/some/path"},
        )
        result = dispatcher._resolve_cwd(listener, trigger)
        assert result == "/some/path"

    def test_uses_listener_resolve_cwd(self):
        """If listener.resolve_cwd returns a path, use it."""
        dispatcher = Dispatcher()
        listener = MagicMock()
        listener.resolve_cwd.return_value = "/listener/path"
        trigger = Trigger(
            platform="discord",
            session_key="discord:123",
            prompt="test",
            reply_context={},
        )
        result = dispatcher._resolve_cwd(listener, trigger)
        assert result == "/listener/path"

    def test_falls_back_to_home_dir(self):
        """If no cwd available, fall back to home dir."""
        dispatcher = Dispatcher()
        listener = MagicMock()
        listener.resolve_cwd.return_value = None
        trigger = Trigger(
            platform="discord",
            session_key="discord:123",
            prompt="test",
            reply_context={},
        )
        result = dispatcher._resolve_cwd(listener, trigger)
        assert result == str(Path.home())

    def test_falls_back_when_listener_raises(self):
        """If listener.resolve_cwd raises, fall back to home dir."""
        dispatcher = Dispatcher()
        listener = MagicMock()
        listener.resolve_cwd.side_effect = RuntimeError("oops")
        trigger = Trigger(
            platform="discord",
            session_key="discord:123",
            prompt="test",
            reply_context={},
        )
        result = dispatcher._resolve_cwd(listener, trigger)
        assert result == str(Path.home())

    def test_reply_context_cwd_takes_precedence(self):
        """reply_context.cwd takes precedence over listener.resolve_cwd."""
        dispatcher = Dispatcher()
        listener = MagicMock()
        listener.resolve_cwd.return_value = "/listener/path"
        trigger = Trigger(
            platform="discord",
            session_key="discord:123",
            prompt="test",
            reply_context={"cwd": "/context/path"},
        )
        result = dispatcher._resolve_cwd(listener, trigger)
        assert result == "/context/path"
        # listener.resolve_cwd should not be called
        listener.resolve_cwd.assert_not_called()
