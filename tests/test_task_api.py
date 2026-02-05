from __future__ import annotations

import asyncio
import contextlib
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator
from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import session as session_module
import task_api


@asynccontextmanager
async def _test_client() -> AsyncIterator[TestClient]:
    app = task_api.create_app()
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


def _make_session(thread_id: int, cwd: str, active: bool = True) -> session_module.ClaudeSession:
    active_session = session_module.ClaudeSession(chat_id=1, thread_id=thread_id, cwd=cwd)
    active_session.active = active
    return active_session


@pytest.mark.asyncio
async def test_health_endpoint() -> None:
    task_api.clear_task_channel_factory()
    with patch.object(task_api.session, "sessions", new={}):
        async with _test_client() as client:
            response = await client.get("/health")
            assert response.status == 200
            payload = await response.json()
            assert payload["status"] == "ok"
            assert payload["sessions"] == 0
            assert payload["factory_registered"] is False


@pytest.mark.asyncio
async def test_sessions_endpoint_empty() -> None:
    task_api.clear_task_channel_factory()
    with patch.object(task_api.session, "sessions", new={}):
        async with _test_client() as client:
            response = await client.get("/sessions")
            assert response.status == 200
            payload = await response.json()
            assert payload == []


@pytest.mark.asyncio
async def test_sessions_endpoint_with_sessions() -> None:
    task_api.clear_task_channel_factory()
    running_task = asyncio.create_task(asyncio.sleep(10))
    session_one = _make_session(thread_id=101, cwd="/tmp/project-a", active=True)
    session_one.current_task = running_task
    session_two = _make_session(thread_id=202, cwd="/tmp/project-b", active=False)
    sessions = {101: session_one, 202: session_two}
    try:
        with patch.object(task_api.session, "sessions", new=sessions):
            async with _test_client() as client:
                response = await client.get("/sessions")
                assert response.status == 200
                payload = await response.json()
                by_thread_id = {item["thread_id"]: item for item in payload}
                assert by_thread_id[101]["cwd"] == "/tmp/project-a"
                assert by_thread_id[101]["active"] is True
                assert by_thread_id[101]["has_running_task"] is True
                assert by_thread_id[202]["cwd"] == "/tmp/project-b"
                assert by_thread_id[202]["active"] is False
                assert by_thread_id[202]["has_running_task"] is False
    finally:
        running_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await running_task


@pytest.mark.asyncio
async def test_inject_missing_prompt() -> None:
    task_api.clear_task_channel_factory()
    with patch.object(task_api.session, "sessions", new={}):
        async with _test_client() as client:
            response = await client.post("/inject", json={})
            assert response.status == 400


@pytest.mark.asyncio
async def test_inject_invalid_json() -> None:
    task_api.clear_task_channel_factory()
    with patch.object(task_api.session, "sessions", new={}):
        async with _test_client() as client:
            response = await client.post(
                "/inject",
                data="{not json}",
                headers={"Content-Type": "application/json"},
            )
            assert response.status == 400


@pytest.mark.asyncio
async def test_inject_no_factory_no_thread() -> None:
    task_api.clear_task_channel_factory()
    with patch.object(task_api.session, "sessions", new={}):
        async with _test_client() as client:
            response = await client.post("/inject", json={"prompt": "hello"})
            assert response.status == 503


@pytest.mark.asyncio
async def test_inject_existing_session() -> None:
    task_api.clear_task_channel_factory()
    active_session = _make_session(thread_id=303, cwd="/tmp/existing")
    sessions = {303: active_session}
    with patch.object(task_api.session, "sessions", new=sessions):
        with patch.object(task_api.session, "start_claude_task", return_value=None) as start_mock:
            async with _test_client() as client:
                response = await client.post(
                    "/inject",
                    json={"prompt": "continue", "thread_id": 303},
                )
                assert response.status == 200
                payload = await response.json()
                assert payload["thread_id"] == 303
                assert payload["cwd"] == "/tmp/existing"
                start_mock.assert_called_once_with(303, "continue")


@pytest.mark.asyncio
async def test_inject_nonexistent_session() -> None:
    task_api.clear_task_channel_factory()
    with patch.object(task_api.session, "sessions", new={}):
        async with _test_client() as client:
            response = await client.post(
                "/inject",
                json={"prompt": "missing", "thread_id": 404},
            )
            assert response.status == 404


@pytest.mark.asyncio
async def test_inject_creates_channel() -> None:
    task_api.clear_task_channel_factory()
    sessions: dict[int, session_module.ClaudeSession] = {}

    async def create_channel(task_name: str) -> int:
        assert task_name.startswith("Task prompt")
        thread_id = 505
        sessions[thread_id] = _make_session(thread_id=thread_id, cwd="/tmp/new")
        return thread_id

    task_api.register_task_channel_factory(create_channel)
    try:
        with patch.object(task_api.session, "sessions", new=sessions):
            with patch.object(task_api.session, "start_claude_task", return_value=None) as start_mock:
                async with _test_client() as client:
                    response = await client.post(
                        "/inject",
                        json={"prompt": "Task prompt for new thread"},
                    )
                    assert response.status == 200
                    payload = await response.json()
                    assert payload["thread_id"] == 505
                    assert payload["cwd"] == "/tmp/new"
                    start_mock.assert_called_once_with(505, "Task prompt for new thread")
    finally:
        task_api.clear_task_channel_factory()


def test_register_factory() -> None:
    async def factory(task_name: str) -> int:
        return 1

    task_api.register_task_channel_factory(factory)
    assert task_api._create_task_channel is factory
    task_api.clear_task_channel_factory()
    assert task_api._create_task_channel is None
