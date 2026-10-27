from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage

from schedule_bot.analytics import UsageAnalytics
from schedule_bot.bot.app import confirm_data_deletion
from schedule_bot.cli import main
from schedule_bot.domain import Lesson, ParsedSchedule
from schedule_bot.notifier import ScheduleNotifier
from schedule_bot.repository import ScheduleRepository


class Clock:
    now = datetime(2026, 10, 13, 10, tzinfo=timezone.utc)


class Bot:
    def __init__(self, errors: list[Exception] | None = None) -> None:
        self.errors = list(errors or [])
        self.sent: list[str] = []
        self.calls = 0

    async def send_message(self, chat_id: int, text: str) -> SimpleNamespace:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        self.sent.append(text)
        return SimpleNamespace(message_id=self.calls)


@pytest.fixture
def queued(tmp_path: Path) -> ScheduleRepository:
    repository = ScheduleRepository(tmp_path / "db.sqlite")
    day = date(2026, 10, 14)
    lesson = Lesson(day, "ИС-22", 1, "1 пара", 1, "09:00", "10:30", "Математика")
    result = repository.import_schedule(ParsedSchedule("14.10.xlsx", day, day, (day,), (lesson,)), sha256="original")
    repository.save_profile(123, role="student", target="ИС-22")
    repository.enqueue_notifications([
        {
            "import_id": result.import_id, "destination_id": 123,
            "destination_kind": "user", "chat_type": "private",
            "chunk_index": index, "text": f"part {index}",
        }
        for index in range(3)
    ], now=Clock.now)
    return repository


def notifier(bot: Bot, repository: ScheduleRepository, clock: Clock, **kwargs) -> ScheduleNotifier:
    return ScheduleNotifier(
        bot, repository, (), broadcast_interval=0, private_interval=0,
        group_interval=0, clock=lambda: clock.now, **kwargs,
    )


def outbox(repository: ScheduleRepository) -> list[dict]:
    with closing(sqlite3.connect(repository.database_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute("SELECT * FROM notification_outbox ORDER BY id")]


@pytest.mark.asyncio
async def test_six_transient_errors_then_recovery_sends_whole_series(queued: ScheduleRepository) -> None:
    clock = Clock()
    bot = Bot([RuntimeError("network") for _ in range(6)])
    for _ in range(6):
        worker = notifier(bot, ScheduleRepository(queued.database_path), clock)
        assert await worker.deliver_pending() == 0
        assert bot.sent == []
        head = outbox(queued)[0]
        assert head["status"] == "pending"
        assert await worker.deliver_pending() == 0
        clock.now = datetime.fromisoformat(head["next_attempt_at"])
    assert bot.calls == 6
    assert await notifier(bot, queued, clock).deliver_pending() == 3
    assert bot.sent == ["part 0", "part 1", "part 2"]
    assert queued.notification_outbox_counts() == {"sent": 3}


@pytest.mark.parametrize("error_type", [TelegramBadRequest, TelegramForbiddenError])
@pytest.mark.asyncio
async def test_permanent_telegram_failure_does_not_send_tail(queued: ScheduleRepository, error_type) -> None:
    bot = Bot([error_type(method=SendMessage(chat_id=123, text="part 0"), message="rejected")])
    worker = notifier(bot, queued, Clock())
    assert await worker.deliver_pending() == 0
    assert await worker.deliver_pending() == 0
    assert bot.calls == 1
    assert bot.sent == []
    assert queued.notification_outbox_counts() == {"failed": 3}
    assert [r["failure_kind"] for r in outbox(queued)] == ["permanent", "blocked", "blocked"]


@pytest.mark.asyncio
async def test_forbidden_chat_is_deactivated(queued: ScheduleRepository) -> None:
    queued.delete_user_data(123, "actor")
    queued.save_chat_profile(-100, chat_type="group", chat_title="ИС-22", target="ИС-22", configured_by=1)
    queued.enqueue_notifications([{
        "import_id": 1, "destination_id": -100, "destination_kind": "chat",
        "chat_type": "group", "chunk_index": 0, "text": "common",
    }], now=Clock.now)
    bot = Bot([TelegramForbiddenError(method=SendMessage(chat_id=-100, text="common"), message="removed")])
    assert await notifier(bot, queued, Clock()).deliver_pending() == 0
    assert queued.get_chat_profile(-100)["active"] is False
    assert outbox(queued)[0]["failure_kind"] == "permanent"


@pytest.mark.asyncio
async def test_deleted_profile_is_rechecked_after_rate_limit_wait(queued: ScheduleRepository) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    bot = Bot()
    analytics = UsageAnalytics(queued, "secret")
    worker = notifier(bot, queued, Clock(), analytics=analytics)

    async def wait_slot(*args) -> None:
        entered.set()
        await release.wait()

    worker._wait_send_slot = wait_slot
    task = asyncio.create_task(worker.deliver_pending())
    await asyncio.wait_for(entered.wait(), timeout=2)
    assert await asyncio.wait_for(worker.delete_user_data(123, analytics.actor_key("user", 123)), timeout=2)
    release.set()
    assert await asyncio.wait_for(task, timeout=2) == 0
    assert bot.calls == 0
    assert outbox(queued) == []
    assert (await analytics.summary())["active_actors"] == 0


@pytest.mark.parametrize("phase", ["api", "analytics"])
@pytest.mark.asyncio
async def test_deletion_waits_for_inflight_api_and_statistics_across_notifiers(queued: ScheduleRepository, phase: str) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class GateBot(Bot):
        async def send_message(self, chat_id: int, text: str) -> SimpleNamespace:
            if phase == "api":
                entered.set()
                await release.wait()
            return await super().send_message(chat_id, text)

    class GateAnalytics(UsageAnalytics):
        async def track(self, *args, **kwargs) -> None:
            if phase == "analytics":
                entered.set()
                await release.wait()
            await super().track(*args, **kwargs)

    bot = GateBot()
    analytics = GateAnalytics(queued, "secret")
    worker = notifier(bot, queued, Clock(), analytics=analytics)
    # A separate notifier/repository must use the same destination lock.
    deleter = notifier(bot, ScheduleRepository(queued.database_path), Clock())
    sending = asyncio.create_task(worker.deliver_pending())
    await asyncio.wait_for(entered.wait(), timeout=2)
    deleting = asyncio.create_task(deleter.delete_user_data(123, analytics.actor_key("user", 123)))
    await asyncio.sleep(0)
    assert not deleting.done()
    release.set()
    assert await asyncio.wait_for(deleting, timeout=2)
    await asyncio.wait_for(sending, timeout=2)
    assert bot.sent == ["part 0"]
    assert outbox(queued) == []
    assert queued.get_profile(123) is None
    assert (await analytics.summary())["active_actors"] == 0
    assert await worker.deliver_pending() == 0
    assert bot.calls == 1


@pytest.mark.asyncio
async def test_delivery_expiring_during_wait_is_not_sent(queued: ScheduleRepository) -> None:
    clock = Clock()
    bot = Bot()
    worker = notifier(bot, queued, clock)

    async def wait_slot(*args) -> None:
        clock.now += ScheduleRepository.NOTIFICATION_TTL

    worker._wait_send_slot = wait_slot
    assert await worker.deliver_pending() == 0
    assert bot.calls == 0
    assert queued.notification_outbox_counts() == {"failed": 3}


@pytest.mark.asyncio
async def test_deletion_during_retry_after_prevents_second_api_request(queued: ScheduleRepository) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    clock = Clock()
    bot = Bot([TelegramRetryAfter(method=SendMessage(chat_id=123, text="part 0"), message="limited", retry_after=60)])
    analytics = UsageAnalytics(queued, "secret")
    worker = notifier(bot, queued, clock, analytics=analytics)
    waits = 0

    async def wait_slot(*args) -> None:
        nonlocal waits
        waits += 1
        if waits == 2:
            entered.set()
            await release.wait()
            clock.now += timedelta(seconds=60)

    worker._wait_send_slot = wait_slot
    sending = asyncio.create_task(worker.deliver_pending())
    await asyncio.wait_for(entered.wait(), timeout=2)
    assert outbox(queued)[0]["attempts"] == 1
    assert await asyncio.wait_for(worker.delete_user_data(123, analytics.actor_key("user", 123)), timeout=2)
    release.set()
    assert await asyncio.wait_for(sending, timeout=2) == 0
    assert bot.calls == 1
    assert bot.sent == []
    assert outbox(queued) == []
    assert (await analytics.summary())["active_actors"] == 0


@pytest.mark.asyncio
async def test_analytics_failure_does_not_retry_successful_send(queued: ScheduleRepository) -> None:
    analytics = SimpleNamespace(track=AsyncMock(side_effect=RuntimeError("statistics unavailable")))
    bot = Bot()
    worker = notifier(bot, queued, Clock(), analytics=analytics)
    assert await worker.deliver_pending() == 3
    assert await worker.deliver_pending() == 0
    assert bot.sent == ["part 0", "part 1", "part 2"]
    assert queued.notification_outbox_counts() == {"sent": 3}


@pytest.mark.asyncio
async def test_concurrent_notifiers_recheck_candidates_under_shared_lock(queued: ScheduleRepository) -> None:
    bot = Bot()
    first = notifier(bot, queued, Clock())
    second = notifier(bot, ScheduleRepository(queued.database_path), Clock())
    results = await asyncio.gather(first.deliver_pending(), second.deliver_pending())
    assert sum(results) == 3
    assert bot.sent == ["part 0", "part 1", "part 2"]


@pytest.mark.asyncio
async def test_delete_confirmation_happens_after_queue_and_events_are_purged(queued: ScheduleRepository) -> None:
    analytics = UsageAnalytics(queued, "secret")
    await analytics.track("bot_started", actor_id=123, chat_type="private")
    bot = Bot()

    async def answer(text, **kwargs) -> None:
        assert text == "Ваши данные удалены."
        assert queued.get_profile(123) is None
        assert outbox(queued) == []
        assert (await analytics.summary())["active_actors"] == 0

    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=123), answer=AsyncMock(),
        message=SimpleNamespace(answer=AsyncMock(side_effect=answer), edit_reply_markup=AsyncMock()),
    )
    state = SimpleNamespace(clear=AsyncMock())
    await confirm_data_deletion(callback, queued, analytics, state, notifier(bot, queued, Clock()))
    state.clear.assert_awaited_once()
    callback.message.answer.assert_awaited_once()
    callback.answer.assert_awaited_once()
    assert await notifier(bot, queued, Clock()).deliver_pending() == 0


def test_cli_redrive_requeues_only_unsent_chunks(queued: ScheduleRepository, tmp_path: Path, monkeypatch, capsys) -> None:
    first, second, _ = outbox(queued)
    queued.mark_notification_sent(first["id"])
    queued.mark_notification_failed(second["id"], "rejected", permanent=True, now=Clock.now)
    monkeypatch.setattr("sys.argv", ["schedule-bot", "retry-notifications", "1", "123", "--kind", "user"])
    monkeypatch.setattr("schedule_bot.cli.Settings.from_env", lambda **kwargs: SimpleNamespace(database_path=queued.database_path, backups_path=tmp_path / "backups"))
    main()
    assert "chunks queued for retry: 2" in capsys.readouterr().out
    assert [r["status"] for r in outbox(queued)] == ["sent", "pending", "pending"]
