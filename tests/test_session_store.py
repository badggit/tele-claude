"""Tests for core/session_store.py."""

import json
import time
from pathlib import Path

import pytest

from core.session_store import SessionStore
from core.types import PersistedSession


class TestSessionStore:
    """Tests for SessionStore."""

    def test_load_creates_empty_store_if_file_missing(self, tmp_path: Path):
        """If store file doesn't exist, start with empty sessions."""
        store = SessionStore(path=tmp_path / "sessions.json")
        store.load()
        assert store.get("any_key") is None

    def test_save_and_load_roundtrip(self, tmp_path: Path):
        """Sessions can be saved and loaded."""
        store_path = tmp_path / "sessions.json"
        store = SessionStore(path=store_path)
        store.load()

        store.update_session_id(
            session_key="discord:123",
            claude_session_id="sess_abc",
            cwd="/home/user",
            platform="discord",
        )

        # Create new store instance and load
        store2 = SessionStore(path=store_path)
        store2.load()

        result = store2.get("discord:123")
        assert result is not None
        assert result.claude_session_id == "sess_abc"
        assert result.cwd == "/home/user"
        assert result.platform == "discord"

    def test_get_returns_none_for_missing_key(self, tmp_path: Path):
        """get() returns None for non-existent keys."""
        store = SessionStore(path=tmp_path / "sessions.json")
        store.load()
        assert store.get("nonexistent") is None

    def test_update_increments_message_count(self, tmp_path: Path):
        """Updating same session increments message_count."""
        store = SessionStore(path=tmp_path / "sessions.json")
        store.load()

        store.update_session_id("key", "sess1", "/cwd", "discord")
        result = store.get("key")
        assert result is not None
        assert result.message_count == 1

        store.update_session_id("key", "sess1", "/cwd", "discord")
        result = store.get("key")
        assert result is not None
        assert result.message_count == 2

    def test_update_preserves_created_at(self, tmp_path: Path):
        """Updating session preserves original created_at."""
        store = SessionStore(path=tmp_path / "sessions.json")
        store.load()

        store.update_session_id("key", "sess1", "/cwd", "discord")
        result = store.get("key")
        assert result is not None
        created_at = result.created_at

        time.sleep(0.01)
        store.update_session_id("key", "sess1", "/cwd", "discord")

        result = store.get("key")
        assert result is not None
        assert result.created_at == created_at

    def test_remove_deletes_session(self, tmp_path: Path):
        """remove() deletes a session."""
        store = SessionStore(path=tmp_path / "sessions.json")
        store.load()

        store.update_session_id("key", "sess1", "/cwd", "discord")
        assert store.get("key") is not None

        store.remove("key")
        assert store.get("key") is None

    def test_cleanup_expired_removes_old_sessions(self, tmp_path: Path):
        """cleanup_expired removes sessions older than max_age."""
        store = SessionStore(path=tmp_path / "sessions.json", max_age_seconds=1)
        store.load()

        store.update_session_id("key", "sess1", "/cwd", "discord")
        # Manually set old timestamp
        store._sessions["key"] = PersistedSession(
            claude_session_id="sess1",
            cwd="/cwd",
            platform="discord",
            created_at=time.time() - 100,
            last_activity=time.time() - 100,
            message_count=1,
        )
        store.save()

        removed = store.cleanup_expired()
        assert removed == 1
        assert store.get("key") is None

    def test_load_handles_corrupt_json(self, tmp_path: Path):
        """Corrupt JSON file results in empty store."""
        store_path = tmp_path / "sessions.json"
        store_path.write_text("not valid json {{{")

        store = SessionStore(path=store_path)
        store.load()
        assert store.get("any") is None

    def test_load_handles_wrong_version(self, tmp_path: Path):
        """Wrong version number results in empty store."""
        store_path = tmp_path / "sessions.json"
        store_path.write_text(json.dumps({"version": 999, "sessions": {}}))

        store = SessionStore(path=store_path)
        store.load()
        assert store.get("any") is None

    def test_update_with_empty_session_id_does_nothing(self, tmp_path: Path):
        """Empty session_id is ignored."""
        store = SessionStore(path=tmp_path / "sessions.json")
        store.load()

        store.update_session_id("key", "", "/cwd", "discord")
        assert store.get("key") is None
