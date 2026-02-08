from __future__ import annotations

import time
from json import JSONDecodeError
from typing import Any, Optional

from aiohttp import ContentTypeError, web

from config import TASK_API_HOST, TASK_API_PORT
from core.dispatcher import Dispatcher
from core.types import Trigger, make_session_key

_dispatcher: Optional[Dispatcher] = None
_runner: Optional[web.AppRunner] = None
_site: Optional[web.TCPSite] = None


def register_dispatcher(dispatcher: Dispatcher) -> None:
    global _dispatcher
    _dispatcher = dispatcher


def clear_dispatcher() -> None:
    global _dispatcher
    _dispatcher = None


def _get_dispatcher(request: Optional[web.Request] = None) -> Optional[Dispatcher]:
    if request is not None:
        dispatcher = request.app.get("dispatcher")
        if dispatcher is not None:
            return dispatcher
    return _dispatcher


def _get_session_state(session) -> str:
    if session.pending_permission:
        return "awaiting_permission"
    if session.current_task and not session.current_task.done():
        return "processing"
    return "idle"


def _session_payload(session) -> dict[str, Any]:
    thread_id = getattr(session.claude_session, "thread_id", None)
    stats = session.stats
    return {
        "session_key": session.session_key,
        "platform": session.platform,
        "thread_id": thread_id,
        "cwd": session.cwd,
        "state": _get_session_state(session),
        "stats": {
            "created_at": stats.created_at,
            "last_activity": stats.last_activity,
            "uptime_seconds": max(0.0, time.time() - stats.created_at),
            "message_count": stats.message_count,
            "turn_count": stats.turn_count,
            "interrupt_count": stats.interrupt_count,
            "error_count": stats.error_count,
        },
    }


async def handle_inject(request: web.Request) -> web.Response:
    """POST /inject - inject a prompt into an existing or new session."""
    dispatcher = _get_dispatcher(request)
    if dispatcher is None:
        return web.json_response({"error": "dispatcher_not_ready"}, status=503)

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

    session_key_value = payload.get("session_key")
    if session_key_value is not None:
        if not isinstance(session_key_value, str):
            return web.json_response({"error": "invalid_session_key"}, status=400)
        session = dispatcher.sessions.get(session_key_value)
        if not session:
            return web.json_response({"error": "session_not_found"}, status=404)
        trigger = Trigger(
            platform=session.platform,
            session_key=session_key_value,
            prompt=prompt,
            reply_context={},
            source="task_api",
        )
        await dispatcher.route_trigger(trigger)
        return web.json_response({"status": "injected", "session_key": session_key_value})

    platform = payload.get("platform")
    if platform not in ("telegram", "discord"):
        return web.json_response({"error": "platform_required"}, status=400)

    if platform == "telegram":
        chat_id = payload.get("chat_id")
        if not isinstance(chat_id, int) or isinstance(chat_id, bool):
            return web.json_response({"error": "chat_id_required"}, status=400)

        thread_id = payload.get("thread_id")
        if thread_id is not None and (not isinstance(thread_id, int) or isinstance(thread_id, bool)):
            return web.json_response({"error": "invalid_thread_id"}, status=400)

        if thread_id is None:
            listener = dispatcher.get_listener("telegram")
            if not listener or not hasattr(listener, "create_topic"):
                return web.json_response({"error": "telegram_listener_unavailable"}, status=503)
            topic_name = payload.get("topic_name")
            if not isinstance(topic_name, str) or not topic_name.strip():
                topic_name = "Task"
            thread_id = await listener.create_topic(chat_id, topic_name)  # type: ignore[call-arg]

        session_key = make_session_key("telegram", chat_id=chat_id, thread_id=thread_id)
        reply_context = {"chat_id": chat_id, "thread_id": thread_id}

    else:
        channel_id = payload.get("channel_id")
        if not isinstance(channel_id, int) or isinstance(channel_id, bool):
            return web.json_response({"error": "channel_id_required"}, status=400)
        session_key = make_session_key("discord", channel_id=channel_id)
        reply_context = {"channel_id": channel_id}

    trigger = Trigger(
        platform=platform,
        session_key=session_key,
        prompt=prompt,
        reply_context=reply_context,
        source="task_api",
    )
    await dispatcher.route_trigger(trigger)
    return web.json_response({"status": "injected", "session_key": session_key})


async def handle_sessions(request: web.Request) -> web.Response:
    """GET /sessions - list active sessions."""
    dispatcher = _get_dispatcher(request)
    if dispatcher is None:
        return web.json_response([], status=200)
    return web.json_response([_session_payload(item) for item in dispatcher.sessions.values()])


async def handle_session_detail(request: web.Request) -> web.Response:
    """GET /sessions/{key} - get single session details."""
    dispatcher = _get_dispatcher(request)
    if dispatcher is None:
        return web.json_response({"error": "dispatcher_not_ready"}, status=503)
    key = request.match_info.get("key")
    if not key:
        return web.json_response({"error": "missing_key"}, status=400)
    session = dispatcher.sessions.get(key)
    if not session:
        return web.json_response({"error": "not_found"}, status=404)
    return web.json_response(_session_payload(session))


async def handle_health(request: web.Request) -> web.Response:
    """GET /health - basic health check."""
    dispatcher = _get_dispatcher(request)
    return web.json_response(
        {
            "status": "ok",
            "sessions": len(dispatcher.sessions) if dispatcher else 0,
            "dispatcher_ready": dispatcher is not None,
        }
    )


def create_app(dispatcher: Optional[Dispatcher] = None) -> web.Application:
    """Create the aiohttp application for the task API."""
    app = web.Application()
    app["dispatcher"] = dispatcher
    app.router.add_post("/inject", handle_inject)
    app.router.add_get("/sessions", handle_sessions)
    app.router.add_get("/sessions/{key}", handle_session_detail)
    app.router.add_get("/health", handle_health)
    return app


async def start_task_api(
    dispatcher: Dispatcher,
    host: str = TASK_API_HOST,
    port: int = TASK_API_PORT,
) -> web.AppRunner:
    """Start the HTTP server. Called from dispatcher/main."""
    global _runner, _site
    if _runner is not None:
        return _runner

    register_dispatcher(dispatcher)
    app = create_app(dispatcher)
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
