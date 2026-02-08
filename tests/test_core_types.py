from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.types import ReplyCapabilities, SessionStats, Trigger, make_session_key


def test_make_session_key_telegram_without_thread() -> None:
    assert make_session_key("telegram", chat_id=123) == "telegram:123"


def test_make_session_key_telegram_with_thread() -> None:
    assert make_session_key("telegram", chat_id=123, thread_id=456) == "telegram:123:456"


def test_make_session_key_discord() -> None:
    assert make_session_key("discord", channel_id=789) == "discord:789"


def test_make_session_key_unknown_platform() -> None:
    with pytest.raises(ValueError):
        make_session_key("slack", channel_id=1)


def test_trigger_defaults() -> None:
    trigger = Trigger(platform="telegram", session_key="telegram:1", prompt="hi")
    assert trigger.images == []
    assert trigger.reply_context == {}
    assert trigger.source == "user"


def test_session_stats_defaults() -> None:
    before = time.time()
    stats = SessionStats()
    after = time.time()
    # Check timestamps are reasonable (created between before and after)
    assert before <= stats.created_at <= after
    assert before <= stats.last_activity <= after
    assert stats.message_count == 0
    assert stats.turn_count == 0
    assert stats.interrupt_count == 0
    assert stats.error_count == 0


def test_reply_capabilities_defaults() -> None:
    caps = ReplyCapabilities()
    assert caps.can_edit is True
    assert caps.can_buttons is True
    assert caps.can_typing is True
    assert caps.max_length == 4000
    assert caps.max_buttons_per_row == 3
