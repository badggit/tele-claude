from __future__ import annotations

import asyncio
import contextlib
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Optional, cast

import pytest
from aiohttp.test_utils import TestClient, TestServer

PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.session_actor import SessionActor
from core.types import Trigger, make_session_key
import task_api


class _DummySession:
    def __init__(self, thread_id: int) -> None:
        self.thread_id = thread_id


class _FakeDispatcher:
    def __init__(self) -> None:
        self.sessions: dict[str, SessionActor] = {}
        self.routed: list[Trigger] = []
        self._listeners: dict[str, object] = {}

    def get_listener(self, platform: str):
        return self._listeners.get(platform)

    async def route_trigger(self, trigger: Trigger) -> None:
        self.routed.append(trigger)


class _FakeTelegramListener:
    def __init__(self, thread_id: int = 222) -> None:
        self.thread_id = thread_id
        self.called_with: Optional[tuple[int, str]] = None

    async def create_topic(self, chat_id: int, topic_name: str) -> int:
        self.called_with = (chat_id, topic_name)
        return self.thread_id


@asynccontextmanager
async def _test_client(dispatcher: Optional[_FakeDispatcher] = None) -> AsyncIterator[TestClient]:
    app = task_api.create_app(cast(Any, dispatcher))
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


def _make_actor(session_key: str, platform: str, thread_id: int, cwd: str) -> SessionActor:
    return SessionActor(
        session_key=session_key,
        platform=platform,
        cwd=cwd,
        reply_target=cast(Any, object()),
        claude_session=_DummySession(thread_id),
    )


@pytest.mark.asyncio
async def test_health_endpoint_without_dispatcher() -> None:
    async with _test_client() as client:
        response = await client.get("/health")
        assert response.status == 200
        payload = await response.json()
        assert payload["status"] == "ok"
        assert payload["sessions"] == 0
        assert payload["dispatcher_ready"] is False


@pytest.mark.asyncio
async def test_sessions_endpoint_empty() -> None:
    dispatcher = _FakeDispatcher()
    async with _test_client(dispatcher) as client:
        response = await client.get("/sessions")
        assert response.status == 200
        payload = await response.json()
        assert payload == []


@pytest.mark.asyncio
async def test_sessions_endpoint_with_sessions() -> None:
    dispatcher = _FakeDispatcher()
    running_task = asyncio.create_task(asyncio.sleep(10))
    session_one = _make_actor("telegram:1", "telegram", thread_id=101, cwd="/tmp/project-a")
    session_one.current_task = running_task
    session_two = _make_actor("discord:2", "discord", thread_id=202, cwd="/tmp/project-b")
    dispatcher.sessions = {
        session_one.session_key: session_one,
        session_two.session_key: session_two,
    }
    try:
        async with _test_client(dispatcher) as client:
            response = await client.get("/sessions")
            assert response.status == 200
            payload = await response.json()
            by_key = {item["session_key"]: item for item in payload}
            assert by_key["telegram:1"]["cwd"] == "/tmp/project-a"
            assert by_key["telegram:1"]["state"] == "processing"
            assert by_key["discord:2"]["cwd"] == "/tmp/project-b"
            assert by_key["discord:2"]["state"] == "idle"
    finally:
        running_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await running_task


@pytest.mark.asyncio
async def test_session_detail_not_found() -> None:
    dispatcher = _FakeDispatcher()
    async with _test_client(dispatcher) as client:
        response = await client.get("/sessions/missing")
        assert response.status == 404


@pytest.mark.asyncio
async def test_inject_missing_prompt() -> None:
    dispatcher = _FakeDispatcher()
    async with _test_client(dispatcher) as client:
        response = await client.post("/inject", json={})
        assert response.status == 400


@pytest.mark.asyncio
async def test_inject_invalid_json() -> None:
    dispatcher = _FakeDispatcher()
    async with _test_client(dispatcher) as client:
        response = await client.post(
            "/inject",
            data="{not json}",
            headers={"Content-Type": "application/json"},
        )
        assert response.status == 400


@pytest.mark.asyncio
async def test_inject_without_dispatcher() -> None:
    async with _test_client() as client:
        response = await client.post("/inject", json={"prompt": "hello"})
        assert response.status == 503


@pytest.mark.asyncio
async def test_inject_existing_session() -> None:
    dispatcher = _FakeDispatcher()
    session_key = "telegram:123"
    dispatcher.sessions[session_key] = _make_actor(session_key, "telegram", thread_id=123, cwd="/tmp/existing")
    async with _test_client(dispatcher) as client:
        response = await client.post("/inject", json={"prompt": "continue", "session_key": session_key})
        assert response.status == 200
        payload = await response.json()
        assert payload["session_key"] == session_key
        assert len(dispatcher.routed) == 1
        assert dispatcher.routed[0].session_key == session_key


@pytest.mark.asyncio
async def test_inject_creates_telegram_topic() -> None:
    dispatcher = _FakeDispatcher()
    listener = _FakeTelegramListener(thread_id=505)
    dispatcher._listeners["telegram"] = listener
    async with _test_client(dispatcher) as client:
        response = await client.post(
            "/inject",
            json={
                "prompt": "Task prompt for new thread",
                "platform": "telegram",
                "chat_id": 1,
                "topic_name": "Task prompt",
            },
        )
        assert response.status == 200
        payload = await response.json()
        expected_key = make_session_key("telegram", chat_id=1, thread_id=505)
        assert payload["session_key"] == expected_key
        assert listener.called_with == (1, "Task prompt")
