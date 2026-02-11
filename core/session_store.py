from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from threading import Lock
from typing import Optional

from core.types import PersistedSession

_log = logging.getLogger("tele-claude.session_store")

_STORE_VERSION = 1
_DEFAULT_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
_DEFAULT_STORE_PATH = Path(__file__).resolve().parents[1] / ".bot-sessions.json"


class SessionStore:
    """Persistent session metadata store."""

    def __init__(self, path: Optional[Path] = None, max_age_seconds: int = _DEFAULT_MAX_AGE_SECONDS) -> None:
        self._path = path or _DEFAULT_STORE_PATH
        self._max_age_seconds = max_age_seconds
        self._sessions: dict[str, PersistedSession] = {}
        self._lock = Lock()

    def load(self) -> None:
        """Load session data from disk."""
        with self._lock:
            self._sessions = {}
            if not self._path.exists():
                _log.info("Session store file not found at %s", self._path)
                return

            try:
                raw = self._path.read_text(encoding="utf-8")
                if not raw.strip():
                    return
                data = json.loads(raw)
            except Exception as exc:
                _log.warning("Failed to load session store: %s", exc)
                return

            if not isinstance(data, dict):
                _log.warning("Session store has invalid format")
                return
            if data.get("version") != _STORE_VERSION:
                _log.warning("Session store version mismatch: %s", data.get("version"))
                return

            sessions = data.get("sessions", {})
            if not isinstance(sessions, dict):
                _log.warning("Session store sessions invalid format")
                return

            for session_key, payload in sessions.items():
                if not isinstance(payload, dict):
                    _log.warning("Skipping invalid session entry for %s", session_key)
                    continue
                try:
                    claude_session_id = str(payload["claude_session_id"])
                    cwd = str(payload["cwd"])
                    platform = str(payload["platform"])
                    created_at = float(payload["created_at"])
                    last_activity = float(payload["last_activity"])
                    message_count = int(payload.get("message_count", 0))
                except Exception as exc:
                    _log.warning("Skipping invalid session entry for %s: %s", session_key, exc)
                    continue
                self._sessions[str(session_key)] = PersistedSession(
                    claude_session_id=claude_session_id,
                    cwd=cwd,
                    platform=platform,
                    created_at=created_at,
                    last_activity=last_activity,
                    message_count=message_count,
                )
            _log.info("Loaded %d persisted sessions from %s", len(self._sessions), self._path)

    def save(self) -> None:
        """Persist session data to disk (atomic)."""
        with self._lock:
            self._save_unlocked()

    def get(self, session_key: str) -> Optional[PersistedSession]:
        """Lookup a persisted session."""
        with self._lock:
            result = self._sessions.get(session_key)
            if result:
                _log.info("Found persisted session for %s: session_id=%s", session_key, result.claude_session_id[:8] + "...")
            else:
                _log.debug("No persisted session for %s (available: %s)", session_key, list(self._sessions.keys()))
            return result

    def update_session_id(
        self,
        session_key: str,
        claude_session_id: str,
        cwd: str,
        platform: str,
    ) -> None:
        """Update or create a persisted session entry."""
        if not claude_session_id:
            return
        now = time.time()
        with self._lock:
            existing = self._sessions.get(session_key)
            created_at = existing.created_at if existing else now
            message_count = (existing.message_count + 1) if existing else 1
            self._sessions[session_key] = PersistedSession(
                claude_session_id=claude_session_id,
                cwd=cwd,
                platform=platform,
                created_at=created_at,
                last_activity=now,
                message_count=message_count,
            )
            self._save_unlocked()

    def remove(self, session_key: str) -> None:
        """Remove a persisted session entry."""
        with self._lock:
            if session_key in self._sessions:
                self._sessions.pop(session_key, None)
                self._save_unlocked()

    def cleanup_expired(self) -> int:
        """Remove sessions inactive for longer than the expiry window."""
        cutoff = time.time() - self._max_age_seconds
        with self._lock:
            expired = [key for key, sess in self._sessions.items() if sess.last_activity < cutoff]
            for key in expired:
                self._sessions.pop(key, None)
            if expired:
                self._save_unlocked()
            return len(expired)

    def _serialize(self) -> dict[str, object]:
        return {
            "version": _STORE_VERSION,
            "sessions": {
                key: {
                    "claude_session_id": session.claude_session_id,
                    "cwd": session.cwd,
                    "platform": session.platform,
                    "created_at": session.created_at,
                    "last_activity": session.last_activity,
                    "message_count": session.message_count,
                }
                for key, session in self._sessions.items()
            },
        }

    def _save_unlocked(self) -> None:
        data = self._serialize()
        tmp_path: Optional[Path] = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self._path.parent,
                delete=False,
            ) as tmp_file:
                json.dump(data, tmp_file, indent=2)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())
                tmp_path = Path(tmp_file.name)
            os.replace(tmp_path, self._path)
        except Exception as exc:
            _log.error("Failed to save session store: %s", exc)
            if tmp_path and tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass


session_store = SessionStore()
