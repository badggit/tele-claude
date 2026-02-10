from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.session_actor import SessionActor
from core.types import Trigger


async def _wait_for_condition(predicate, timeout: float = 1.0) -> None:
    async def _wait() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_wait(), timeout=timeout)


@pytest.mark.asyncio
async def test_run_loop_processes_trigger_and_increments_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    import session as session_module

    def fake_start(thread_id: int, prompt: str, bot) -> asyncio.Task:
        return asyncio.create_task(asyncio.sleep(0))

    monkeypatch.setattr(session_module, "start_claude_task", fake_start)

    actor = SessionActor(
        session_key="telegram:1",
        platform="telegram",
        cwd="/tmp",
        reply_target=MagicMock(),
        claude_session=MagicMock(thread_id=1, bot=None, pending_image_path=None),
    )

    await actor.start()
    assert actor._run_loop_task is not None

    trigger = Trigger(platform="telegram", session_key="telegram:1", prompt="hi")
    await actor.enqueue(trigger)

    await _wait_for_condition(lambda: actor.current_task is not None)
    if actor.current_task:
        await actor.current_task

    assert actor.stats.message_count == 1
    assert actor._generation_id == 1

    actor.active = False
    actor._run_loop_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await actor._run_loop_task


@pytest.mark.asyncio
async def test_interrupt_cancels_running_task_and_increments_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    import session as session_module

    actor = SessionActor(
        session_key="telegram:2",
        platform="telegram",
        cwd="/tmp",
        reply_target=MagicMock(),
        claude_session=MagicMock(thread_id=2, bot=None, pending_image_path=None),
    )

    blocker = asyncio.Event()

    def fake_start(thread_id: int, prompt: str, bot) -> asyncio.Task:
        return asyncio.create_task(blocker.wait())

    async def fake_interrupt(thread_id: int) -> bool:
        if actor.current_task:
            actor.current_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await actor.current_task
        return True

    monkeypatch.setattr(session_module, "start_claude_task", fake_start)
    monkeypatch.setattr(session_module, "interrupt_session", fake_interrupt)

    await actor.start()
    await actor.enqueue(Trigger(platform="telegram", session_key="telegram:2", prompt="first"))
    await _wait_for_condition(lambda: actor.current_task is not None)

    await actor.enqueue(Trigger(platform="telegram", session_key="telegram:2", prompt="second"))
    await _wait_for_condition(lambda: actor._generation_id == 3)

    assert actor.stats.interrupt_count == 1
    assert actor._generation_id == 3

    actor.active = False
    await actor._cancel_current_task()
    if actor._run_loop_task:
        actor._run_loop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await actor._run_loop_task


@pytest.mark.asyncio
async def test_cancel_current_task_cancels_pending_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    import session as session_module

    async def fake_interrupt(thread_id: int) -> bool:
        return False

    monkeypatch.setattr(session_module, "interrupt_session", fake_interrupt)

    actor = SessionActor(
        session_key="telegram:3",
        platform="telegram",
        cwd="/tmp",
        reply_target=MagicMock(),
        claude_session=MagicMock(thread_id=3, bot=None, pending_image_path=None),
    )

    actor.current_task = asyncio.create_task(asyncio.sleep(10))
    pending = asyncio.get_running_loop().create_future()
    actor.pending_permission = pending

    await actor._cancel_current_task()

    assert pending.cancelled() is True
    assert actor.pending_permission is None
    assert actor.current_task is None


@pytest.mark.asyncio
async def test_model_command_sets_override_and_skips_claude_task(monkeypatch: pytest.MonkeyPatch) -> None:
    import session as session_module
    import config

    started = False

    def fake_start(thread_id: int, prompt: str, bot) -> asyncio.Task:
        nonlocal started
        started = True
        return asyncio.create_task(asyncio.sleep(0))

    monkeypatch.setattr(session_module, "start_claude_task", fake_start)

    reply_target = MagicMock()
    reply_target.send = AsyncMock(return_value=MagicMock())

    actor = SessionActor(
        session_key="discord:42",
        platform="discord",
        cwd="/tmp",
        reply_target=reply_target,
        claude_session=MagicMock(
            thread_id=42,
            bot=None,
            pending_image_path=None,
            contextual_commands=[],
            model_override=None,
        ),
    )

    model_name = config.AVAILABLE_MODELS[0]
    await actor._handle_prompt(f"/model {model_name}", images=[], gen_id=1)

    assert getattr(actor.claude_session, "model_override") == model_name
    assert started is False
    reply_target.send.assert_awaited_once()
    sent_message = reply_target.send.await_args.args[0]
    assert f"Model switched to `{model_name}`" in sent_message.text
