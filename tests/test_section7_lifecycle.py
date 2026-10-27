import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import SetMyCommands

from schedule_bot.bot import app
from schedule_bot.bot.signals import ScheduleSignalListener

pytest_plugins = ("test_stage4_background_jobs",)
pytestmark = pytest.mark.asyncio


async def test_configure_commands_network_failure_retries_then_polls(runtime):
    error = TelegramNetworkError(method=SetMyCommands(commands=[]), message="offline")
    app.configure_commands.side_effect = [error, None]
    await app.run_bot(runtime.settings)
    assert app.configure_commands.await_count == 2
    runtime.dispatcher.start_polling.assert_awaited_once()
    assert runtime.scheduler.stopped
    runtime.bot.session.close.assert_awaited_once()


async def test_signal_reaches_running_listener_and_worker_is_stopped(runtime):
    event = asyncio.Event()
    runtime.bot.id = 999

    async def update(*args, **kwargs):
        if kwargs.get("wait_for_lock"):
            event.set()

    runtime.update.side_effect = update

    async def polling(bot, **kwargs):
        repo = kwargs["repository"]
        repo.save_chat_profile(-100, chat_type="supergroup", chat_title="signal",
                               target="A", configured_by=1)
        message = SimpleNamespace(chat=SimpleNamespace(id=-100, type="supergroup"),
                                  from_user=SimpleNamespace(id=5, is_bot=True, username="source"),
                                  message_id=1, text="changed", caption=None, document=None)
        await runtime.listeners[0].receive(message, bot)
        await asyncio.wait_for(event.wait(), 5)

    runtime.dispatcher.start_polling.side_effect = polling
    await app.run_bot(runtime.settings)
    assert sum(bool(call.kwargs.get("wait_for_lock")) for call in runtime.update.await_args_list) == 1
    assert runtime.scheduler.stopped
    runtime.bot.session.close.assert_awaited_once()


@pytest.mark.xfail(strict=True, reason="startup scheduler failure bypasses worker and session cleanup")
@pytest.mark.parametrize("resource", ["worker", "session", "card_service"])
async def test_startup_scheduler_failure_closes_already_created_worker(runtime, monkeypatch, resource):
    tasks = []
    async def running_worker(self):
        tasks.append(asyncio.current_task())
        await asyncio.Event().wait()

    monkeypatch.setattr(app.GrokReactionWorker, "run", running_worker)
    error = RuntimeError("scheduler registration failed")
    runtime.scheduler.add_job = lambda *args, **kwargs: (_ for _ in ()).throw(error)
    try:
        with pytest.raises(RuntimeError, match="scheduler registration failed") as caught:
            await app.run_bot(runtime.settings)
        assert caught.value is error
        await asyncio.sleep(0)
        if resource == "worker":
            assert not tasks or all(task.done() for task in tasks)
        elif resource == "session":
            runtime.bot.session.close.assert_awaited_once()
        else:
            app.close_card_service.assert_awaited_once()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("stage", [
    *[pytest.param(stage, marks=pytest.mark.xfail(strict=True, reason="startup before finally leaks already created resources"))
      for stage in ("scheduler_constructor", "scheduler_start", "dispatcher", "listener", "router")],
    "commands", "polling",
])
async def test_startup_exception_preserves_cause_and_cleans_resources(runtime, monkeypatch, stage):
    before = asyncio.all_tasks()
    error = RuntimeError(f"injected {stage}")
    def fail(*args, **kwargs):
        raise error
    if stage == "scheduler_constructor":
        monkeypatch.setattr(app, "AsyncIOScheduler", fail)
    elif stage == "scheduler_start":
        runtime.scheduler.start = fail
    elif stage == "dispatcher":
        monkeypatch.setattr(app, "Dispatcher", fail)
    elif stage == "listener":
        monkeypatch.setattr(app, "ScheduleSignalListener", fail)
    elif stage == "router":
        runtime.dispatcher.include_router = Mock(side_effect=error)
    else:
        operation = app.configure_commands if stage == "commands" else runtime.dispatcher.start_polling
        operation.side_effect = error
    try:
        with pytest.raises(RuntimeError) as caught:
            await app.run_bot(runtime.settings)
        assert caught.value is error
        await asyncio.sleep(0)
        assert not (asyncio.all_tasks() - before)
        runtime.bot.session.close.assert_awaited_once()
        app.close_card_service.assert_awaited_once()
        if stage not in {"scheduler_constructor", "scheduler_start"}:
            assert runtime.scheduler.stopped
    finally:
        remaining = asyncio.all_tasks() - before
        for task in remaining:
            task.cancel()
        await asyncio.gather(*remaining, return_exceptions=True)


@pytest.mark.parametrize("worker", [
    *[pytest.param(worker, marks=pytest.mark.xfail(strict=True, reason="failed worker masks polling exception and interrupts cleanup"))
      for worker in ("signal", "health", "delivery", "startup")],
    "grok",
])
async def test_failed_worker_does_not_mask_polling_exception_or_skip_cleanup(runtime, monkeypatch, worker):
    before = asyncio.all_tasks()
    failed = asyncio.Event()
    primary = ValueError("polling failure")
    secondary = RuntimeError("worker failure")
    async def fail(*args, **kwargs):
        failed.set()
        raise secondary
    if worker == "signal":
        monkeypatch.setattr(ScheduleSignalListener, "run", fail)
    elif worker == "health":
        monkeypatch.setattr("schedule_bot.runtime_health.run_runtime_health", fail)
    elif worker == "delivery":
        monkeypatch.setattr(app.ScheduleNotifier, "run_delivery_worker", fail)
    elif worker == "grok":
        monkeypatch.setattr(app.GrokReactionWorker, "run", fail)
    else:
        runtime.update.side_effect = fail
    async def polling(*args, **kwargs):
        await asyncio.wait_for(failed.wait(), 5)
        raise primary
    runtime.dispatcher.start_polling.side_effect = polling
    try:
        with pytest.raises(Exception) as caught:
            await app.run_bot(runtime.settings)
        assert caught.value is primary
        runtime.bot.session.close.assert_awaited_once()
        app.close_card_service.assert_awaited_once()
        assert runtime.scheduler.stopped
        assert not (asyncio.all_tasks() - before)
    finally:
        remaining = asyncio.all_tasks() - before
        for task in remaining:
            task.cancel()
        await asyncio.gather(*remaining, return_exceptions=True)


@pytest.mark.xfail(strict=True, reason="second cancellation interrupts card cleanup before session.close")
async def test_repeated_cancellation_still_closes_session(runtime, monkeypatch):
    polling, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def poll(*args, **kwargs):
        polling.set()
        await asyncio.Event().wait()
    async def close():
        closing.set()
        await release.wait()
    runtime.dispatcher.start_polling.side_effect = poll
    app.close_card_service.side_effect = close
    task = asyncio.create_task(app.run_bot(runtime.settings))
    try:
        await asyncio.wait_for(polling.wait(), 5)
        task.cancel()
        await asyncio.wait_for(closing.wait(), 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        runtime.bot.session.close.assert_awaited_once()
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("stage", [
    pytest.param("scheduler", marks=pytest.mark.xfail(strict=True, reason="scheduler shutdown failure skips remaining cleanup")),
    pytest.param("cards", marks=pytest.mark.xfail(strict=True, reason="card service close failure skips session.close")),
    "session",
])
async def test_cleanup_failure_does_not_leave_other_resources_open(runtime, monkeypatch, stage):
    before = asyncio.all_tasks()
    error = RuntimeError(f"close {stage}")
    def fail(*args, **kwargs):
        raise error
    if stage == "scheduler":
        runtime.scheduler.shutdown = fail
    else:
        operation = app.close_card_service if stage == "cards" else runtime.bot.session.close
        operation.side_effect = error
    try:
        with pytest.raises(RuntimeError) as caught:
            await app.run_bot(runtime.settings)
        assert caught.value is error
        runtime.bot.session.close.assert_awaited_once()
        app.close_card_service.assert_awaited_once()
        assert not (asyncio.all_tasks() - before)
    finally:
        remaining = asyncio.all_tasks() - before
        for task in remaining:
            task.cancel()
        await asyncio.gather(*remaining, return_exceptions=True)
