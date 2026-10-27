from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

pytestmark = pytest.mark.usefixtures("text_schedule_media")

from schedule_bot import config
from schedule_bot.bot.app import automatic_update_job, manual_update
from schedule_bot.downloader import (
    DownloadedSchedule,
    ScheduleDownloadError,
    ScheduleLink,
)
from schedule_bot.notifier import ScheduleNotifier
from schedule_bot.outage import ScheduleOutageTracker
from schedule_bot.parser import ExcelScheduleParser
from schedule_bot.repository import ScheduleRepository
from schedule_bot.services import ScheduleUpdater


def source_error(category="http_502") -> ScheduleDownloadError:
    return ScheduleDownloadError(
        "HTTP 502 Bad Gateway", transient=True, category=category
    )


@pytest.fixture
def system(tmp_path, schedule_workbook, monkeypatch):
    monkeypatch.setattr(config, "_load_dotenv", lambda: None)
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "99")
    settings = config.Settings.from_env(require_bot_token=False)
    repository = ScheduleRepository(tmp_path / "schedule.db")
    repository.initialize()
    bot = SimpleNamespace(send_message=AsyncMock())
    now = SimpleNamespace(value=datetime(2026, 10, 14, 10, tzinfo=timezone.utc))

    class RepositoryDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.value.astimezone(tz) if tz else now.value.replace(tzinfo=None)

    monkeypatch.setattr("schedule_bot.repository.datetime", RepositoryDateTime)
    notifier = ScheduleNotifier(
        bot,
        repository,
        (99,),
        broadcast_interval=0,
        private_interval=0,
        group_interval=0,
        clock=lambda: now.value,
    )
    tracker = ScheduleOutageTracker(
        repository,
        notifier,
        "https://example.test/9006/",
        clock=lambda: now.value,
    )
    link = ScheduleLink(
        "https://example.test/file.xlsx",
        schedule_workbook.name,
        date(2026, 9, 21),
        date(2026, 9, 23),
    )
    downloaded = DownloadedSchedule(
        link,
        schedule_workbook,
        hashlib.sha256(schedule_workbook.read_bytes()).hexdigest(),
    )
    downloader = SimpleNamespace(fetch_latest=AsyncMock(return_value=downloaded))
    updater = ScheduleUpdater(
        downloader, ExcelScheduleParser(), repository, outage_tracker=tracker
    )
    return SimpleNamespace(
        settings=settings,
        repository=repository,
        bot=bot,
        notifier=notifier,
        tracker=tracker,
        downloader=downloader,
        updater=updater,
        analytics=SimpleNamespace(track=AsyncMock()),
        lock=asyncio.Lock(),
        pin_job=AsyncMock(),
        now=now,
        downloaded=downloaded,
    )


async def automatic(system) -> None:
    await automatic_update_job(
        system.updater, system.notifier, system.analytics, system.lock, system.pin_job
    )


def message() -> SimpleNamespace:
    return SimpleNamespace(from_user=SimpleNamespace(id=99), answer=AsyncMock())


async def manual(system, request) -> None:
    await manual_update(
        request,
        system.bot,
        system.settings,
        system.repository,
        system.updater,
        system.notifier,
        system.lock,
        system.analytics,
        pin_sync_lock=asyncio.Lock(),
    )


@pytest.mark.asyncio
async def test_failed_fetch_preserves_schedule_imports_and_user_delivery(system):
    system.repository.save_profile(123, role="student", target="ПД-12")
    await automatic(system)
    assert system.repository.notification_outbox_counts() == {"sent": 3}
    before = system.repository.lessons_for(
        role="student", target="ПД-12", lesson_date=date(2026, 9, 22)
    )
    assert before
    with closing(sqlite3.connect(system.repository.database_path)) as connection, connection:
        imports = connection.execute("SELECT COUNT(*) FROM imports").fetchone()[0]
    system.downloader.fetch_latest.side_effect = source_error()
    for _ in range(6):
        system.now.value += timedelta(minutes=30)
        await automatic(system)
    assert system.tracker.get_state()["consecutive_failures"] == 6
    assert (
        system.repository.lessons_for(
            role="student", target="ПД-12", lesson_date=date(2026, 9, 22)
        )
        == before
    )
    with closing(sqlite3.connect(system.repository.database_path)) as connection, connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == imports
        )
    assert system.repository.notification_outbox_counts() == {"sent": 3}
    assert (
        sum(call.args[0] == 123 for call in system.bot.send_message.call_args_list) == 3
    )
    assert system.pin_job.await_count == 1


@pytest.mark.asyncio
async def test_one_automatic_check_counts_once_and_manual_failure_does_not_count(
    system,
):
    system.downloader.fetch_latest.side_effect = source_error()
    await automatic(system)
    assert system.tracker.get_state()["consecutive_failures"] == 1
    await manual(system, request := message())
    assert system.tracker.get_state()["consecutive_failures"] == 1
    assert "Ошибка обновления" in request.answer.call_args.args[0]


@pytest.mark.asyncio
async def test_successful_manual_update_recovers_without_duplicate_import(
    system, monkeypatch
):
    monkeypatch.setattr("schedule_bot.bot.app.sync_schedule_pins", AsyncMock())
    await automatic(system)
    system.downloader.fetch_latest.side_effect = source_error()
    for _ in range(3):
        await automatic(system)
    await system.notifier.deliver_admin_pending()
    assert system.bot.send_message.await_count == 1
    system.downloader.fetch_latest.side_effect = None
    await manual(system, request := message())
    assert system.tracker.get_state()["status"] == "HEALTHY"
    await system.notifier.deliver_admin_pending()
    assert system.bot.send_message.await_count == 2
    assert request.answer.call_args.args[0] == "Новых файлов нет."
    await manual(system, message())
    await system.notifier.deliver_admin_pending()
    assert system.bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_manual_update_uses_configured_pin_freshness_threshold(system, monkeypatch):
    pins = AsyncMock()
    monkeypatch.setattr("schedule_bot.bot.app.sync_schedule_pins", pins)
    system.settings = replace(system.settings, schedule_stale_after_minutes=17)
    await manual(system, message())
    pins.assert_awaited_once()
    assert pins.await_args.kwargs["stale_after_minutes"] == 17


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["parser", "database", "delivery", "pins"])
async def test_internal_errors_are_critical_and_never_count_as_source_outage(
    system, monkeypatch, phase
):
    if phase == "parser":
        monkeypatch.setattr(
            system.updater.parser, "parse", Mock(side_effect=ValueError("bad workbook"))
        )
    elif phase == "database":
        monkeypatch.setattr(
            system.repository,
            "import_schedule",
            Mock(side_effect=sqlite3.DatabaseError("broken database")),
        )
    elif phase == "delivery":
        system.notifier.notify_import = AsyncMock(side_effect=source_error())
    else:
        system.pin_job.side_effect = source_error()
    system.notifier.notify_admins_error = AsyncMock()
    # Even broken analytics must not suppress the critical notification.
    system.analytics.track.side_effect = RuntimeError("analytics unavailable")
    await automatic(system)
    assert system.tracker.get_state()["status"] == "HEALTHY"
    assert system.tracker.get_state()["consecutive_failures"] == 0
    system.notifier.notify_admins_error.assert_awaited_once()


@pytest.mark.asyncio
async def test_outage_storage_failure_reaches_critical_alert(system):
    system.downloader.fetch_latest.side_effect = source_error()
    system.tracker.record_failure = AsyncMock(
        side_effect=sqlite3.DatabaseError("write failed")
    )
    system.notifier.notify_admins_error = AsyncMock()
    await automatic(system)
    system.notifier.notify_admins_error.assert_awaited_once()
    assert isinstance(
        system.notifier.notify_admins_error.call_args.args[0], sqlite3.DatabaseError
    )


@pytest.mark.asyncio
async def test_permanent_http_failure_is_reported_immediately(system):
    system.downloader.fetch_latest.side_effect = ScheduleDownloadError(
        "HTTP 404 Not Found", category="http_404"
    )
    system.notifier.notify_admins_error = AsyncMock()
    await automatic(system)
    assert system.tracker.get_state()["consecutive_failures"] == 0
    system.notifier.notify_admins_error.assert_awaited_once()


@pytest.mark.asyncio
async def test_overlapping_automatic_checks_are_skipped(system):
    entered, release = asyncio.Event(), asyncio.Event()

    async def fetch():
        entered.set()
        await release.wait()
        raise source_error()

    system.downloader.fetch_latest.side_effect = fetch
    first = asyncio.create_task(automatic(system))
    await entered.wait()
    await automatic(system)
    release.set()
    await first
    assert system.downloader.fetch_latest.await_count == 1
    assert system.tracker.get_state()["consecutive_failures"] == 1


@pytest.mark.asyncio
async def test_manual_and_automatic_updates_share_lock(system, monkeypatch):
    monkeypatch.setattr("schedule_bot.bot.app.sync_schedule_pins", AsyncMock())
    entered, release = asyncio.Event(), asyncio.Event()

    async def fetch():
        entered.set()
        await release.wait()
        return system.downloaded

    system.downloader.fetch_latest.side_effect = fetch
    first = asyncio.create_task(automatic(system))
    await entered.wait()
    second = asyncio.create_task(manual(system, request := message()))
    await asyncio.sleep(0)
    assert system.downloader.fetch_latest.await_count == 1
    release.set()
    await asyncio.gather(first, second)
    assert system.downloader.fetch_latest.await_count == 2
    assert request.answer.call_args.args[0] == "Новых файлов нет."
    with closing(sqlite3.connect(system.repository.database_path)) as connection, connection:
        assert connection.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_canceled_automatic_check_leaves_state_and_lock_intact(system):
    entered = asyncio.Event()

    async def fetch():
        entered.set()
        await asyncio.Event().wait()

    system.downloader.fetch_latest.side_effect = fetch
    task = asyncio.create_task(automatic(system))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not system.lock.locked()
    assert system.tracker.get_state()["consecutive_failures"] == 0
    system.analytics.track.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_reply_survives_broken_analytics(system):
    system.downloader.fetch_latest.side_effect = source_error()
    system.analytics.track.side_effect = RuntimeError("analytics unavailable")
    await manual(system, request := message())
    assert "Ошибка обновления" in request.answer.call_args.args[0]
