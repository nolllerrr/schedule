from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import GetUpdates
from apscheduler.events import (
    EVENT_JOB_ERROR,
    EVENT_JOB_EXECUTED,
    EVENT_JOB_MAX_INSTANCES,
    EVENT_SCHEDULER_SHUTDOWN,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from schedule_bot import config
from schedule_bot.bot import app
from schedule_bot.bot.signals import ScheduleSignalListener

REAL_AUTOMATIC_UPDATE_JOB = app.automatic_update_job


class RecordingScheduler:
    def __init__(self, **kwargs):
        self.jobs = []
        self.started = False
        self.stopped = False
        self.listeners = []

    @property
    def running(self):
        return self.started and not self.stopped

    def pause(self):
        pass

    def add_listener(self, listener, mask):
        self.listeners.append(listener)

    def remove_listener(self, listener):
        self.listeners.remove(listener)

    def add_job(self, function, **kwargs):
        self.jobs.append((function, kwargs))

    def start(self):
        self.started = True

    def shutdown(self, *, wait):
        assert wait is False
        self.stopped = True
        for listener in self.listeners[:]:
            listener(SimpleNamespace(code=EVENT_SCHEDULER_SHUTDOWN))


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_load_dotenv", lambda: None)
    settings = replace(
        config.Settings.from_env(require_bot_token=False),
        database_path=tmp_path / "runtime.db", backups_path=tmp_path / "backups",
        downloads_path=tmp_path / "downloads", telegram_retry_seconds=0,
        schedule_signal_bot_username="source", schedule_signal_chat_id=-100,
    )
    scheduler = RecordingScheduler()
    monkeypatch.setattr(app, "AsyncIOScheduler", lambda **kwargs: scheduler)
    bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock()))
    monkeypatch.setattr(app, "Bot", lambda *args, **kwargs: bot)
    monkeypatch.setattr(app, "create_telegram_session", Mock(return_value=bot.session))
    monkeypatch.setattr(app, "configure_commands", AsyncMock())
    monkeypatch.setattr(app, "close_card_service", AsyncMock())
    notifier_errors = AsyncMock()
    monkeypatch.setattr(app.ScheduleNotifier, "notify_admins_error", notifier_errors)
    pins = AsyncMock()
    monkeypatch.setattr(app, "sync_schedule_pins", pins)
    update = AsyncMock()
    monkeypatch.setattr(app, "automatic_update_job", update)
    monkeypatch.setattr(app.ScheduleNotifier, "run_delivery_worker", AsyncMock())
    listeners = []
    listener_type = app.ScheduleSignalListener

    def listener_factory(*args, **kwargs):
        listener = listener_type(*args, **kwargs)
        listeners.append(listener)
        return listener

    monkeypatch.setattr(app, "ScheduleSignalListener", listener_factory)
    monkeypatch.setattr(app, "create_signal_router", lambda listener: object())
    dispatcher = SimpleNamespace(include_router=Mock(), start_polling=AsyncMock())
    monkeypatch.setattr(app, "Dispatcher", lambda **kwargs: dispatcher)
    return SimpleNamespace(settings=settings, scheduler=scheduler, bot=bot, pins=pins,
                           update=update, errors=notifier_errors, listeners=listeners,
                           dispatcher=dispatcher)


@pytest.mark.asyncio
async def test_background_jobs_isolate_failures_recover_and_obey_locks(runtime, monkeypatch):
    backup = app.create_database_backup
    cleanup = app.cleanup_technical_data
    backup_calls = Mock(side_effect=[OSError("backup failed"), None])
    cleanup_calls = Mock(side_effect=[OSError("cleanup failed"), None])

    def backup_once(*args, **kwargs):
        backup_calls()
        return backup(*args, **kwargs)

    def cleanup_once(*args, **kwargs):
        cleanup_calls()
        return cleanup(*args, **kwargs)

    monkeypatch.setattr(app, "create_database_backup", backup_once)
    monkeypatch.setattr(app, "cleanup_technical_data", cleanup_once)
    pin_error = RuntimeError("pin failed")
    runtime.pins.side_effect = [pin_error, None, None]

    async def polling(bot, **kwargs):
        await asyncio.sleep(0)
        jobs = runtime.scheduler.jobs
        assert len(jobs) == 5
        assert all(options["max_instances"] == 1 and options["coalesce"] for _, options in jobs)
        assert [options["trigger"] for _, options in jobs] == ["interval", "interval", "cron", "cron", "cron"]
        update_job, pin_job, backup_job, _, cleanup_job = [job for job, _ in jobs]
        await pin_job()
        await backup_job()
        await cleanup_job()
        await backup_job()
        assert len(list(runtime.settings.backups_path.glob("schedule-*.db"))) == 1
        async with kwargs["update_lock"]:
            blocked_cleanup = asyncio.create_task(cleanup_job())
            await asyncio.sleep(0)
            assert cleanup_calls.call_count == 1
        await blocked_cleanup
        await update_job()
        await runtime.listeners[0].update_job()
        assert runtime.update.await_args_list[-2].kwargs.get("wait_for_lock", False) is False
        assert runtime.update.await_args_list[-1].kwargs["wait_for_lock"] is True

    runtime.dispatcher.start_polling.side_effect = polling
    await app.run_bot(runtime.settings)
    assert runtime.scheduler.started and runtime.scheduler.stopped
    assert backup_calls.call_count == cleanup_calls.call_count == 2
    assert runtime.errors.await_count == 3
    assert [call.kwargs["context"] for call in runtime.errors.await_args_list] == [
        "обновления закреплённого расписания", "резервного копирования базы данных", "очистки технических данных"
    ]
    runtime.bot.session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_real_scheduled_update_skips_overlapping_startup_update(runtime, monkeypatch):
    tasks_before = asyncio.all_tasks()
    scheduler = AsyncIOScheduler(timezone=runtime.settings.timezone)
    monkeypatch.setattr(app, "AsyncIOScheduler", lambda **kwargs: scheduler)
    monkeypatch.setattr(app, "automatic_update_job", REAL_AUTOMATIC_UPDATE_JOB)
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    scheduled = asyncio.Event()
    stopped = asyncio.Event()
    calls = 0

    async def update_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(app.ScheduleUpdater, "update_once", update_once)

    def listener(event):
        if event.code == EVENT_JOB_EXECUTED:
            scheduled.set()
        elif event.code == EVENT_SCHEDULER_SHUTDOWN:
            stopped.set()

    scheduler.add_listener(listener, EVENT_JOB_EXECUTED | EVENT_SCHEDULER_SHUTDOWN)

    async def polling(bot, **kwargs):
        await asyncio.wait_for(entered.wait(), 5)
        update_job = next(job for job in scheduler.get_jobs() if job.func.__name__ == "update_job")
        scheduler.reschedule_job(update_job.id, trigger="interval", seconds=0.05)
        await asyncio.wait_for(scheduled.wait(), 5)
        scheduler.pause_job(update_job.id)
        assert calls == 1

    runtime.dispatcher.start_polling.side_effect = polling
    await app.run_bot(runtime.settings)
    await asyncio.wait_for(stopped.wait(), 5)
    assert cancelled.is_set()
    assert not scheduler._executors["default"]._pending_futures
    assert asyncio.all_tasks() <= tasks_before


@pytest.mark.asyncio
async def test_polling_network_failure_retries_without_duplicate_commands(runtime, monkeypatch):
    error = TelegramNetworkError(method=GetUpdates(), message="offline")
    runtime.dispatcher.start_polling.side_effect = [error, None]
    await app.run_bot(runtime.settings)
    assert runtime.dispatcher.start_polling.await_count == 2
    app.configure_commands.assert_awaited_once()
    assert len(runtime.scheduler.jobs) == 5
    assert runtime.scheduler.stopped
    runtime.bot.session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancellation_stops_workers_scheduler_and_session(runtime, monkeypatch):
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    worker_started = {name: asyncio.Event() for name in ("signal", "delivery", "grok", "health")}
    worker_stopped = set()

    def worker(name):
        async def run(*args, **kwargs):
            worker_started[name].set()
            try:
                await asyncio.Event().wait()
            finally:
                worker_stopped.add(name)
        return run

    monkeypatch.setattr(ScheduleSignalListener, "run", worker("signal"))
    monkeypatch.setattr(app.ScheduleNotifier, "run_delivery_worker", worker("delivery"))
    monkeypatch.setattr(app.GrokReactionWorker, "run", worker("grok"))
    monkeypatch.setattr("schedule_bot.runtime_health.run_runtime_health", worker("health"))

    async def polling(bot, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    runtime.dispatcher.start_polling.side_effect = polling
    task = asyncio.create_task(app.run_bot(runtime.settings))
    await asyncio.wait_for(entered.wait(), timeout=5)
    await asyncio.wait_for(asyncio.gather(*(event.wait() for event in worker_started.values())), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set() and runtime.scheduler.stopped
    assert worker_stopped == set(worker_started)
    runtime.bot.session.close.assert_awaited_once()
    app.close_card_service.assert_awaited_once()

@pytest.mark.asyncio
async def test_overlapping_pin_jobs_are_serialized(runtime):
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def sync(*args, **kwargs):
        calls.append("start")
        if len(calls) == 1:
            entered.set()
            await release.wait()
        calls.append("end")

    runtime.pins.side_effect = sync

    async def polling(bot, **kwargs):
        await asyncio.wait_for(entered.wait(), timeout=5)
        second = asyncio.create_task(runtime.scheduler.jobs[1][0]())
        await asyncio.sleep(0)
        assert calls == ["start"]
        release.set()
        await second
        assert calls == ["start", "end", "start", "end"]

    runtime.dispatcher.start_polling.side_effect = polling
    await app.run_bot(runtime.settings)
    assert runtime.pins.await_count == 2


@pytest.mark.asyncio
async def test_real_scheduler_runs_jobs_isolates_errors_and_stops(runtime, monkeypatch):
    tasks_before = asyncio.all_tasks()
    scheduler = AsyncIOScheduler(timezone=runtime.settings.timezone)
    monkeypatch.setattr(app, "AsyncIOScheduler", lambda **kwargs: scheduler)
    startup_update = asyncio.Event()
    startup_pin = asyncio.Event()
    pin_entered = asyncio.Event()
    release_pin = asyncio.Event()
    pin_finished = asyncio.Event()
    job_error = asyncio.Event()
    max_instances = asyncio.Event()
    shutdown = asyncio.Event()
    errors = []
    unexpected = asyncio.Event()
    pin_calls = 0
    update_calls = 0
    job_ids = {}

    async def update(*args, **kwargs):
        nonlocal update_calls
        update_calls += 1
        if update_calls == 1:
            startup_update.set()
        else:
            raise RuntimeError("scheduled failure")

    async def pin(*args, **kwargs):
        nonlocal pin_calls
        pin_calls += 1
        if pin_calls == 1:
            startup_pin.set()
        else:
            pin_entered.set()
            try:
                await release_pin.wait()
            finally:
                pin_finished.set()

    runtime.update.side_effect = update
    runtime.pins.side_effect = pin

    def listener(event):
        if event.code == EVENT_JOB_ERROR and event.job_id == job_ids.get("update"):
            errors.append(event.exception)
            job_error.set()
        elif event.code == EVENT_JOB_MAX_INSTANCES and event.job_id == job_ids.get("pin"):
            max_instances.set()
        elif event.code == EVENT_SCHEDULER_SHUTDOWN:
            shutdown.set()

    scheduler.add_listener(listener, EVENT_JOB_ERROR | EVENT_JOB_MAX_INSTANCES | EVENT_SCHEDULER_SHUTDOWN)

    async def polling(bot, **kwargs):
        await asyncio.wait_for(asyncio.gather(startup_update.wait(), startup_pin.wait()), 5)
        jobs = scheduler.get_jobs()
        assert len(jobs) == 5
        assert all(job.max_instances == 1 and job.coalesce for job in jobs)
        update_job = next(job for job in jobs if job.func.__name__ == "update_job")
        pin_job = next(job for job in jobs if job.func.__name__ == "pin_job")
        job_ids.update(update=update_job.id, pin=pin_job.id)
        scheduler.reschedule_job(update_job.id, trigger="interval", seconds=0.05)
        scheduler.reschedule_job(pin_job.id, trigger="interval", seconds=0.05)
        try:
            await asyncio.wait_for(asyncio.gather(job_error.wait(), pin_entered.wait(), max_instances.wait()), 5)
            scheduler.pause_job(update_job.id)
            scheduler.pause_job(pin_job.id)
            assert pin_calls == 2
            release_pin.set()
            await asyncio.wait_for(pin_finished.wait(), 5)
            scheduler.add_job(unexpected.set, "date", run_date=datetime.now(UTC) + timedelta(seconds=0.3))
        finally:
            release_pin.set()

    runtime.dispatcher.start_polling.side_effect = polling
    await app.run_bot(runtime.settings)
    await asyncio.wait_for(shutdown.wait(), 5)
    await asyncio.sleep(0.4)
    assert not unexpected.is_set()
    assert not scheduler.running
    assert not scheduler._executors["default"]._pending_futures
    assert errors and all(isinstance(error, RuntimeError) for error in errors)
    assert pin_finished.is_set()
    runtime.bot.session.close.assert_awaited_once()
    assert asyncio.all_tasks() <= tasks_before
