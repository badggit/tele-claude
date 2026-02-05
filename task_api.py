from __future__ import annotations

from json import JSONDecodeError
from typing import Any, Awaitable, Callable, Optional

from aiohttp import ContentTypeError, web

import session
from config import TASK_API_HOST, TASK_API_PORT

CreateTaskChannel = Callable[[str], Awaitable[int]]

_create_task_channel: Optional[CreateTaskChannel] = None
_runner: Optional[web.AppRunner] = None
_site: Optional[web.TCPSite] = None


def register_task_channel_factory(factory: CreateTaskChannel) -> None:
    """Called by platform runners to register their channel factory."""
    global _create_task_channel
    _create_task_channel = factory


def clear_task_channel_factory() -> None:
    """Clear the registered task channel factory."""
    global _create_task_channel
    _create_task_channel = None


def _default_task_name(prompt: str) -> str:
    return prompt[:50]


def _session_has_running_task(active_session: session.ClaudeSession) -> bool:
    current_task = active_session.current_task
    return current_task is not None and not current_task.done()


def _session_payload(active_session: session.ClaudeSession) -> dict[str, Any]:
    return {
        "thread_id": active_session.thread_id,
        "cwd": active_session.cwd,
        "active": active_session.active,
        "has_running_task": _session_has_running_task(active_session),
    }


async def handle_inject(request: web.Request) -> web.Response:
    """POST /inject - inject a prompt into an existing or new session."""
    try:
        payload = await request.json()
    except (JSONDecodeError, ContentTypeError):
        return web.json_response({"error": "invalid_json"}, status=400)

    if not isinstance(payload, dict):
        return web.json_response({"error": "invalid_json"}, status=400)

    prompt_value = payload.get("prompt")
    if not isinstance(prompt_value, str) or not prompt_value.strip():
        return web.json_response({"error": "missing_prompt"}, status=400)
    prompt = prompt_value

    thread_id_value = payload.get("thread_id")
    if thread_id_value is not None:
        if not isinstance(thread_id_value, int) or isinstance(thread_id_value, bool):
            return web.json_response({"error": "invalid_thread_id"}, status=400)
        thread_id = thread_id_value
        active_session = session.sessions.get(thread_id)
        if not active_session:
            return web.json_response({"error": "session_not_found"}, status=404)
        session.start_claude_task(thread_id, prompt)
        return web.json_response(
            {"status": "injected", "thread_id": thread_id, "cwd": active_session.cwd}
        )

    if _create_task_channel is None:
        return web.json_response({"error": "no_task_channel_factory"}, status=503)

    task_name_value = payload.get("task_name")
    task_name: Optional[str] = None
    if task_name_value is not None:
        if not isinstance(task_name_value, str):
            return web.json_response({"error": "invalid_task_name"}, status=400)
        if task_name_value.strip():
            task_name = task_name_value

    task_name = task_name or _default_task_name(prompt)
    thread_id = await _create_task_channel(task_name)
    session.start_claude_task(thread_id, prompt)
    active_session = session.sessions.get(thread_id)
    cwd = active_session.cwd if active_session else ""
    return web.json_response(
        {"status": "injected", "thread_id": thread_id, "cwd": cwd}
    )


async def handle_sessions(request: web.Request) -> web.Response:
    """GET /sessions - list active sessions."""
    active_sessions = [_session_payload(item) for item in session.sessions.values()]
    return web.json_response(active_sessions)


async def handle_health(request: web.Request) -> web.Response:
    """GET /health - basic health check."""
    return web.json_response(
        {
            "status": "ok",
            "sessions": len(session.sessions),
            "factory_registered": _create_task_channel is not None,
        }
    )


def create_app() -> web.Application:
    """Create the aiohttp application for the task API."""
    app = web.Application()
    app.router.add_post("/inject", handle_inject)
    app.router.add_get("/sessions", handle_sessions)
    app.router.add_get("/health", handle_health)
    return app


async def start_task_api(host: str = TASK_API_HOST, port: int = TASK_API_PORT) -> web.AppRunner:
    """Start the HTTP server. Called from platform runners."""
    global _runner, _site
    if _runner is not None:
        return _runner

    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    _runner = runner
    _site = site
    return runner


async def stop_task_api() -> None:
    """Stop the HTTP server. Called on shutdown."""
    global _runner, _site
    if _site is not None:
        await _site.stop()
        _site = None
    if _runner is not None:
        await _runner.cleanup()
        _runner = None
