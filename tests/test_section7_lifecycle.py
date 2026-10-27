import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import SetMyCommands
from apscheduler.schedulers.asyncio import AsyncIOScheduler

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
    "scheduler_constructor", "scheduler_start", "dispatcher", "listener", "router",
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
    "signal", "health", "delivery", "startup",
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
    "scheduler",
    "cards",
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


@pytest.mark.parametrize("stage", [
    "create_telegram_session", "Bot", "UsageAnalytics", "ScheduleNotifier",
    "ScheduleOutageTracker", "ScheduleUpdater", "GrokReactionWorker",
    "ExpiringMemoryStorage", "create_signal_router",
])
async def test_partial_startup_attempts_all_acquired_cleanup(runtime, monkeypatch, stage):
    before = asyncio.all_tasks()
    error = RuntimeError(f"partial startup: {stage}")
    monkeypatch.setattr(app, stage, Mock(side_effect=error))
    with pytest.raises(RuntimeError) as caught:
        await app.run_bot(runtime.settings)
    assert caught.value is error
    app.close_card_service.assert_awaited_once()
    if stage == "create_telegram_session":
        runtime.bot.session.close.assert_not_awaited()
    else:
        runtime.bot.session.close.assert_awaited_once()
    assert asyncio.all_tasks() <= before


@pytest.mark.parametrize("name", [
    "grok worker", "signal worker", "delivery worker", "startup update",
    "startup pin sync", "health worker",
])
async def test_task_creation_failure_closes_prior_resources(runtime, monkeypatch, name):
    before = asyncio.all_tasks()
    create_task = asyncio.create_task
    error = RuntimeError(f"cannot create {name}")

    def create(coroutine, **kwargs):
        if kwargs.get("name") == name:
            raise error
        return create_task(coroutine, **kwargs)

    monkeypatch.setattr(asyncio, "create_task", create)
    with pytest.raises(RuntimeError) as caught:
        await app.run_bot(runtime.settings)
    assert caught.value is error
    runtime.bot.session.close.assert_awaited_once()
    app.close_card_service.assert_awaited_once()
    assert asyncio.all_tasks() <= before


@pytest.mark.parametrize("primary", [None, ValueError("polling"), asyncio.CancelledError("polling")])
async def test_multiple_cleanup_errors_preserve_first_cause(runtime, caplog, primary):
    before = asyncio.all_tasks()
    errors = [RuntimeError("scheduler close"), ValueError("cards close"), OSError("session close")]
    runtime.scheduler.shutdown = Mock(side_effect=errors[0])
    app.close_card_service.side_effect = errors[1]
    runtime.bot.session.close.side_effect = errors[2]
    runtime.dispatcher.start_polling.side_effect = primary
    expected = primary if primary is not None else errors[0]
    with pytest.raises(type(expected)) as caught:
        await app.run_bot(runtime.settings)
    assert caught.value is expected
    logged = [record.exc_info[1] for record in caplog.records if record.exc_info]
    assert all(error in logged for error in errors)
    runtime.bot.session.close.assert_awaited_once()
    app.close_card_service.assert_awaited_once()
    assert asyncio.all_tasks() <= before


@pytest.mark.parametrize("stage", ["scheduler", "cards", "session"])
async def test_cleanup_cancelled_error_does_not_skip_other_resources(runtime, stage):
    before = asyncio.all_tasks()
    error = asyncio.CancelledError(f"close {stage}")
    if stage == "scheduler":
        runtime.scheduler.shutdown = Mock(side_effect=error)
    else:
        operation = app.close_card_service if stage == "cards" else runtime.bot.session.close
        operation.side_effect = error
    with pytest.raises(asyncio.CancelledError) as caught:
        await app.run_bot(runtime.settings)
    assert caught.value is error
    app.close_card_service.assert_awaited_once()
    runtime.bot.session.close.assert_awaited_once()
    assert asyncio.all_tasks() <= before


@pytest.mark.parametrize("worker", ["signal", "health", "delivery", "startup", "startup_pin", "grok"])
async def test_failed_worker_is_propagated_after_successful_polling(runtime, monkeypatch, worker):
    before = asyncio.all_tasks()
    failed = asyncio.Event()
    error = RuntimeError(f"{worker} failed")

    async def fail(*args, **kwargs):
        failed.set()
        raise error

    if worker == "signal":
        monkeypatch.setattr(ScheduleSignalListener, "run", fail)
    elif worker == "health":
        monkeypatch.setattr("schedule_bot.runtime_health.run_runtime_health", fail)
    elif worker == "delivery":
        monkeypatch.setattr(app.ScheduleNotifier, "run_delivery_worker", fail)
    elif worker == "grok":
        monkeypatch.setattr(app.GrokReactionWorker, "run", fail)
    elif worker == "startup_pin":
        runtime.pins.side_effect = ValueError("pin sync")
        runtime.errors.side_effect = fail
    else:
        runtime.update.side_effect = fail

    async def polling(*args, **kwargs):
        await asyncio.wait_for(failed.wait(), 5)

    runtime.dispatcher.start_polling.side_effect = polling
    with pytest.raises(RuntimeError) as caught:
        await app.run_bot(runtime.settings)
    assert caught.value is error
    runtime.bot.session.close.assert_awaited_once()
    app.close_card_service.assert_awaited_once()
    assert runtime.scheduler.stopped
    assert asyncio.all_tasks() <= before


@pytest.mark.parametrize("stage", ["workers", "scheduler", "cards", "session"])
@pytest.mark.parametrize("primary", [None, ValueError("original polling failure")])
async def test_repeated_cancellation_during_each_cleanup_stage(runtime, monkeypatch, stage, primary):
    before = asyncio.all_tasks()
    entered, release, started = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def close():
        entered.set()
        await release.wait()

    async def worker(self):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await close()

    if stage == "workers":
        monkeypatch.setattr(app.ScheduleNotifier, "run_delivery_worker", worker)
    elif stage == "scheduler":
        def shutdown(*, wait):
            runtime.scheduler.stopped = True
            entered.set()
        runtime.scheduler.shutdown = shutdown
    else:
        operation = app.close_card_service if stage == "cards" else runtime.bot.session.close
        operation.side_effect = close

    async def polling(*args, **kwargs):
        if stage == "workers":
            await asyncio.wait_for(started.wait(), 5)
        if primary is not None:
            raise primary

    runtime.dispatcher.start_polling.side_effect = polling
    task = asyncio.create_task(app.run_bot(runtime.settings))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        for index in range(3):
            task.cancel(f"cleanup cancellation {index}")
            await asyncio.sleep(0)
        assert not task.done()
        release.set()
        if stage == "scheduler":
            for listener in runtime.scheduler.listeners[:]:
                listener(None)
        with pytest.raises(type(primary) if primary is not None else asyncio.CancelledError) as caught:
            await task
        if primary is not None:
            assert caught.value is primary
        else:
            assert caught.value.args == ("cleanup cancellation 0",)
        runtime.bot.session.close.assert_awaited_once()
        app.close_card_service.assert_awaited_once()
        assert asyncio.all_tasks() <= before
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("stage", ["workers", "scheduler", "cards", "session"])
@pytest.mark.parametrize("primary", [None, ValueError("polling"), asyncio.CancelledError("polling")])
async def test_cleanup_deadlines_allow_later_resources_to_close(runtime, monkeypatch, caplog, stage, primary):
    before = asyncio.all_tasks()
    started = asyncio.Event()
    monkeypatch.setattr(app, "_WORKER_SHUTDOWN_SECONDS", 0.03)
    monkeypatch.setattr(app, "_RESOURCE_SHUTDOWN_SECONDS", 0.03)
    monkeypatch.setattr(app, "_CANCEL_GRACE_SECONDS", 0.1)

    async def blocked_close():
        await asyncio.Event().wait()

    async def slow_worker(self):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            # A second cancellation must bound a worker's own cleanup as well.
            await blocked_close()

    if stage == "workers":
        monkeypatch.setattr(app.ScheduleNotifier, "run_delivery_worker", slow_worker)
    elif stage == "scheduler":
        runtime.scheduler.shutdown = Mock()  # No completion event.
    else:
        operation = app.close_card_service if stage == "cards" else runtime.bot.session.close
        operation.side_effect = blocked_close

    async def polling(*args, **kwargs):
        if stage == "workers":
            await asyncio.wait_for(started.wait(), 5)
        if primary is not None:
            raise primary

    runtime.dispatcher.start_polling.side_effect = polling
    with pytest.raises(type(primary) if primary is not None else TimeoutError) as caught:
        await asyncio.wait_for(app.run_bot(runtime.settings), 2)
    if primary is not None:
        assert caught.value is primary
    assert "timed out" in caplog.text
    app.close_card_service.assert_awaited_once()
    runtime.bot.session.close.assert_awaited_once()
    assert asyncio.all_tasks() <= before


async def test_cancellation_during_salt_resolution_closes_session(runtime, monkeypatch):
    before = asyncio.all_tasks()
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()

    def resolve_salt(self):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        return "salt"

    monkeypatch.setattr(type(runtime.settings), "resolve_analytics_salt", resolve_salt)
    task = asyncio.create_task(app.run_bot(runtime.settings))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel("salt cancellation")
        with pytest.raises(asyncio.CancelledError, match="salt cancellation"):
            await task
        app.close_card_service.assert_awaited_once()
        runtime.bot.session.close.assert_awaited_once()
        assert asyncio.all_tasks() <= before
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("pause_fails", [False, True])
async def test_real_scheduler_waits_for_running_job_before_closing_services(runtime, monkeypatch, pause_fails):
    before = asyncio.all_tasks()
    scheduler = AsyncIOScheduler(timezone=runtime.settings.timezone)
    monkeypatch.setattr(app, "AsyncIOScheduler", lambda **kwargs: scheduler)
    entered, finished = asyncio.Event(), asyncio.Event()
    pause_error = RuntimeError("pause failed")
    extra_job_started = False

    async def update(*args, **kwargs):
        if asyncio.current_task().get_name() == "startup update":
            return
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.03)
            finished.set()

    async def extra_job():
        nonlocal extra_job_started
        extra_job_started = True

    async def polling(*args, **kwargs):
        update_job = next(job for job in scheduler.get_jobs() if job.func.__name__ == "update_job")
        runtime.update.side_effect = update
        scheduler.add_job(update_job.func, "date")
        await asyncio.wait_for(entered.wait(), 5)
        if pause_fails:
            def pause():
                # This queued job must be rejected by the shutdown admission guard.
                scheduler.add_job(lifecycle.scheduled(extra_job), "date")
                raise pause_error
            monkeypatch.setattr(scheduler, "pause", pause)

    # Capture the owner to exercise jobs arriving after a failed scheduler pause.
    lifecycle = app._BotLifecycle()
    monkeypatch.setattr(app, "_BotLifecycle", lambda: lifecycle)

    async def close():
        assert finished.is_set()
        assert not scheduler.running

    app.close_card_service.side_effect = close
    runtime.bot.session.close.side_effect = close
    runtime.dispatcher.start_polling.side_effect = polling
    if pause_fails:
        with pytest.raises(RuntimeError) as caught:
            await app.run_bot(runtime.settings)
        assert caught.value is pause_error
    else:
        await app.run_bot(runtime.settings)
    assert finished.is_set()
    assert not extra_job_started
    assert not scheduler.running
    app.close_card_service.assert_awaited_once()
    runtime.bot.session.close.assert_awaited_once()
    assert asyncio.all_tasks() <= before


async def test_worker_ignoring_cancellation_does_not_block_shutdown(runtime, monkeypatch, caplog):
    before = asyncio.all_tasks()
    started, release = asyncio.Event(), asyncio.Event()
    workers = []
    monkeypatch.setattr(app, "_WORKER_SHUTDOWN_SECONDS", 0.03)
    monkeypatch.setattr(app, "_CANCEL_GRACE_SECONDS", 0.03)

    async def stubborn_worker(self):
        workers.append(asyncio.current_task())
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass
        raise RuntimeError("late worker failure")

    async def polling(*args, **kwargs):
        await asyncio.wait_for(started.wait(), 5)

    monkeypatch.setattr(app.ScheduleNotifier, "run_delivery_worker", stubborn_worker)
    runtime.dispatcher.start_polling.side_effect = polling
    try:
        with pytest.raises(TimeoutError, match="delivery worker"):
            await asyncio.wait_for(app.run_bot(runtime.settings), 2)
        app.close_card_service.assert_awaited_once()
        runtime.bot.session.close.assert_awaited_once()
        assert "workers timed out" in caplog.text
        assert workers and not workers[0].done()
    finally:
        release.set()
        await asyncio.gather(*workers, return_exceptions=True)
    assert "late worker failure" in caplog.text
    assert asyncio.all_tasks() <= before
