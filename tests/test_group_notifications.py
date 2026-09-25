from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from time import monotonic
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage

from schedule_bot.notifier import ScheduleNotifier, _split_message
from schedule_bot.parser import ExcelScheduleParser
from schedule_bot.pinning import (
    pin_schedule_message,
    sync_chat_schedule_pin,
    unpin_schedule_messages,
)
from schedule_bot.repository import ScheduleRepository


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.pinned: list[tuple[int, int, bool | None]] = []
        self.unpinned: list[tuple[int, int]] = []

    async def send_message(self, chat_id: int, text: str) -> SimpleNamespace:
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=100 + len(self.sent))

    async def pin_chat_message(
        self,
        chat_id: int,
        message_id: int,
        disable_notification: bool | None = None,
    ) -> bool:
        self.pinned.append((chat_id, message_id, disable_notification))
        return True

    async def unpin_chat_message(
        self,
        chat_id: int,
        *,
        message_id: int,
    ) -> bool:
        self.unpinned.append((chat_id, message_id))
        return True


class FailingBot(FakeBot):
    async def send_message(self, chat_id: int, text: str) -> SimpleNamespace:
        raise RuntimeError("temporary network failure")


class FailOnSecondBot(FakeBot):
    async def send_message(self, chat_id: int, text: str) -> SimpleNamespace:
        if len(self.sent) == 1:
            raise RuntimeError("second chunk failed")
        return await super().send_message(chat_id, text)


class TimedBot(FakeBot):
    def __init__(self) -> None:
        super().__init__()
        self.send_times: list[float] = []

    async def send_message(self, chat_id: int, text: str) -> SimpleNamespace:
        self.send_times.append(monotonic())
        return await super().send_message(chat_id, text)


class RetryOnceBot(FakeBot):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    async def send_message(self, chat_id: int, text: str) -> SimpleNamespace:
        self.attempts += 1
        if self.attempts == 1:
            raise TelegramRetryAfter(
                method=SendMessage(chat_id=chat_id, text=text),
                message="Too Many Requests",
                retry_after=1,
            )
        return await super().send_message(chat_id, text)


@pytest.mark.asyncio
async def test_group_receives_new_schedule_without_pinning_notification(
    tmp_path: Path,
    schedule_workbook: Path,
) -> None:
    repository = ScheduleRepository(tmp_path / "schedule.db")
    parsed = ExcelScheduleParser().parse(schedule_workbook)
    result = repository.import_schedule(parsed, sha256="new-schedule")
    repository.save_chat_profile(
        -100123,
        chat_type="supergroup",
        chat_title="ПД-22",
        target="ПД-22",
        configured_by=42,
        pin_enabled=True,
    )
    bot = FakeBot()

    await ScheduleNotifier(bot, repository, ()).notify_import(result)  # type: ignore[arg-type]

    assert bot.sent
    chat_id, text = bot.sent[0]
    assert chat_id == -100123
    assert "Опубликовано новое расписание" in text
    assert "Криминалистика" in text
    assert bot.pinned == []
    assert repository.get_chat_profile(-100123)["last_pinned_message_id"] is None


@pytest.mark.asyncio
async def test_new_schedule_sends_one_message_per_day_to_user_and_group(
    tmp_path: Path,
    schedule_workbook: Path,
) -> None:
    repository = ScheduleRepository(tmp_path / "schedule.db")
    repository.save_profile(123, role="student", target="ПД-12")
    repository.save_chat_profile(
        -100123,
        chat_type="supergroup",
        chat_title="ПД-12",
        target="ПД-12",
        configured_by=42,
    )
    result = repository.import_schedule(
        ExcelScheduleParser().parse(schedule_workbook), sha256="three-day-schedule"
    )
    bot = FakeBot()

    await ScheduleNotifier(bot, repository, ()).notify_import(result)  # type: ignore[arg-type]

    dates = ("21 сентября", "22 сентября", "23 сентября")
    for destination_id in (123, -100123):
        messages = [text for chat_id, text in bot.sent if chat_id == destination_id]
        assert len(messages) == len(dates)
        for message, expected_date in zip(messages, dates):
            assert "Опубликовано новое расписание" in message
            assert expected_date in message
            assert all(other not in message for other in dates if other != expected_date)
    assert repository.notification_outbox_counts() == {"sent": 6}


@pytest.mark.asyncio
async def test_pin_stays_on_today_until_last_lesson_ends(
    tmp_path: Path,
    schedule_workbook: Path,
) -> None:
    repository = ScheduleRepository(tmp_path / "schedule.db")
    repository.import_schedule(
        ExcelScheduleParser().parse(schedule_workbook),
        sha256="pin-by-time",
    )
    repository.save_chat_profile(
        -100321,
        chat_type="supergroup",
        chat_title="ПД-12",
        target="ПД-12",
        configured_by=42,
        pin_enabled=True,
    )
    bot = FakeBot()

    assert await sync_chat_schedule_pin(  # type: ignore[arg-type]
        bot,
        repository,
        chat_id=-100321,
        target="ПД-12",
        now=datetime(2026, 9, 22, 9, 30),
    )
    assert len(bot.sent) == 1
    assert "22 сентября" in bot.sent[0][1]
    assert "Теория гос. и права" in bot.sent[0][1]

    # Tomorrow is already published, but the current study day is still active.
    assert await sync_chat_schedule_pin(  # type: ignore[arg-type]
        bot,
        repository,
        chat_id=-100321,
        target="ПД-12",
        now=datetime(2026, 9, 22, 10, 29),
    )
    assert len(bot.sent) == 1

    assert await sync_chat_schedule_pin(  # type: ignore[arg-type]
        bot,
        repository,
        chat_id=-100321,
        target="ПД-12",
        now=datetime(2026, 9, 22, 10, 30),
    )
    assert len(bot.sent) == 2
    assert "23 сентября" in bot.sent[1][1]
    assert "Психология" in bot.sent[1][1]
    assert bot.unpinned == [(-100321, 101)]
    assert bot.pinned == [
        (-100321, 101, True),
        (-100321, 102, True),
    ]
    profile = repository.get_chat_profile(-100321)
    assert profile["last_pinned_message_id"] == 102
    assert profile["last_pinned_schedule_date"].isoformat() == "2026-09-23"


@pytest.mark.asyncio
async def test_pin_replaces_only_previous_bot_schedule(tmp_path: Path) -> None:
    repository = ScheduleRepository(tmp_path / "schedule.db")
    repository.save_chat_profile(
        -100456,
        chat_type="group",
        chat_title=None,
        target="ИС-22",
        configured_by=42,
        pin_enabled=True,
    )
    repository.set_chat_last_message(-100456, 10)
    bot = FakeBot()

    assert await pin_schedule_message(  # type: ignore[arg-type]
        bot, repository, chat_id=-100456, message_id=11
    )

    assert bot.unpinned == [(-100456, 10)]
    assert bot.pinned == [(-100456, 11, True)]


@pytest.mark.asyncio
async def test_disabling_pin_unpins_tracked_schedule(tmp_path: Path) -> None:
    repository = ScheduleRepository(tmp_path / "schedule.db")
    repository.save_chat_profile(
        -100789,
        chat_type="supergroup",
        chat_title="ИС-22",
        target="ИС-22",
        configured_by=42,
        pin_enabled=True,
    )
    repository.set_chat_last_message(-100789, 25)
    bot = FakeBot()

    assert await unpin_schedule_messages(  # type: ignore[arg-type]
        bot,
        repository,
        chat_id=-100789,
    )

    assert bot.unpinned == [(-100789, 25)]
    assert repository.get_chat_profile(-100789)["last_pinned_message_id"] is None


def test_oversized_html_paragraph_is_split_into_valid_plain_chunks() -> None:
    chunks = _split_message(f"<b>{'x' * 200}</b>", limit=50)

    assert len(chunks) == 4
    assert all(len(chunk) <= 50 for chunk in chunks)
    assert all("<b>" not in chunk and "</b>" not in chunk for chunk in chunks)


@pytest.mark.asyncio
async def test_pending_notification_survives_notifier_restart(
    tmp_path: Path,
    schedule_workbook: Path,
) -> None:
    repository = ScheduleRepository(tmp_path / "schedule.db")
    repository.save_profile(123, role="student", target="ПД-22")
    parsed = ExcelScheduleParser().parse(schedule_workbook)
    result = repository.import_schedule(parsed, sha256="retry-import")

    await ScheduleNotifier(  # type: ignore[arg-type]
        FailingBot(), repository, ()
    ).notify_import(result)

    assert repository.notification_outbox_counts() == {"pending": 1}

    working_bot = FakeBot()
    await ScheduleNotifier(  # type: ignore[arg-type]
        working_bot, repository, ()
    ).notify_import(result.__class__(result.import_id, True, (), result.parsed))

    assert len(working_bot.sent) == 1
    assert repository.notification_outbox_counts() == {"sent": 1}


@pytest.mark.asyncio
async def test_sent_chunk_is_not_repeated_after_partial_failure(
    tmp_path: Path,
    schedule_workbook: Path,
) -> None:
    repository = ScheduleRepository(tmp_path / "schedule.db")
    parsed = ExcelScheduleParser().parse(schedule_workbook)
    result = repository.import_schedule(parsed, sha256="partial-import")
    assert result.import_id is not None
    repository.enqueue_notifications(
        [
            {
                "import_id": result.import_id,
                "destination_id": 123,
                "destination_kind": "user",
                "chat_type": "private",
                "chunk_index": 0,
                "text": "first",
            },
            {
                "import_id": result.import_id,
                "destination_id": 123,
                "destination_kind": "user",
                "chat_type": "private",
                "chunk_index": 1,
                "text": "second",
            },
        ]
    )

    first_bot = FailOnSecondBot()
    await ScheduleNotifier(  # type: ignore[arg-type]
        first_bot, repository, ()
    ).deliver_pending()
    assert first_bot.sent == [(123, "first")]
    assert repository.notification_outbox_counts() == {"pending": 1, "sent": 1}

    second_bot = FakeBot()
    await ScheduleNotifier(  # type: ignore[arg-type]
        second_bot, repository, ()
    ).deliver_pending()
    assert second_bot.sent == [(123, "second")]
    assert repository.notification_outbox_counts() == {"sent": 2}


@pytest.mark.asyncio
async def test_delivery_worker_drains_backlog_from_previous_run(
    tmp_path: Path,
    schedule_workbook: Path,
) -> None:
    repository = ScheduleRepository(tmp_path / "schedule.db")
    result = repository.import_schedule(
        ExcelScheduleParser().parse(schedule_workbook), sha256="worker-backlog"
    )
    assert result.import_id is not None
    repository.enqueue_notifications(
        {
            "import_id": result.import_id,
            "destination_id": user_id,
            "destination_kind": "user",
            "chat_type": "private",
            "chunk_index": 0,
            "text": "queued before restart",
        }
        for user_id in range(1, 121)
    )
    bot = FakeBot()
    notifier = ScheduleNotifier(  # type: ignore[arg-type]
        bot,
        repository,
        (),
        broadcast_interval=0,
        private_interval=0,
        group_interval=0,
    )
    worker = asyncio.create_task(notifier.run_delivery_worker(idle_seconds=0.01))
    try:
        async def wait_until_drained() -> None:
            while len(bot.sent) < 120:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_until_drained(), timeout=10)
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker

    assert len(bot.sent) == 120
    assert repository.notification_outbox_counts() == {"sent": 120}


@pytest.mark.asyncio
async def test_new_import_wakes_idle_delivery_worker(
    tmp_path: Path,
    schedule_workbook: Path,
) -> None:
    repository = ScheduleRepository(tmp_path / "schedule.db")
    repository.save_profile(123, role="student", target="ПД-22")
    result = repository.import_schedule(
        ExcelScheduleParser().parse(schedule_workbook), sha256="worker-wakeup"
    )
    bot = FakeBot()
    notifier = ScheduleNotifier(  # type: ignore[arg-type]
        bot,
        repository,
        (),
        broadcast_interval=0,
        private_interval=0,
        group_interval=0,
    )
    worker = asyncio.create_task(notifier.run_delivery_worker(idle_seconds=30))
    try:
        await asyncio.sleep(0)
        await notifier.notify_import(result)

        async def wait_until_sent() -> None:
            while not bot.sent:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_until_sent(), timeout=2)
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker

    assert len(bot.sent) == 1
    assert repository.notification_outbox_counts() == {"sent": 1}


@pytest.mark.asyncio
async def test_delivery_respects_per_chat_interval(
    tmp_path: Path,
    schedule_workbook: Path,
) -> None:
    repository = ScheduleRepository(tmp_path / "schedule.db")
    result = repository.import_schedule(
        ExcelScheduleParser().parse(schedule_workbook), sha256="rate-limit"
    )
    assert result.import_id is not None
    repository.enqueue_notifications(
        {
            "import_id": result.import_id,
            "destination_id": 123,
            "destination_kind": "user",
            "chat_type": "private",
            "chunk_index": index,
            "text": f"message {index}",
        }
        for index in range(2)
    )
    bot = TimedBot()
    notifier = ScheduleNotifier(  # type: ignore[arg-type]
        bot,
        repository,
        (),
        broadcast_interval=0,
        private_interval=0.08,
        group_interval=0,
    )

    assert await notifier.deliver_pending() == 2

    assert bot.sent == [(123, "message 0"), (123, "message 1")]
    assert bot.send_times[1] - bot.send_times[0] >= 0.07


@pytest.mark.asyncio
async def test_delivery_waits_for_telegram_retry_after(
    tmp_path: Path,
    schedule_workbook: Path,
) -> None:
    repository = ScheduleRepository(tmp_path / "schedule.db")
    result = repository.import_schedule(
        ExcelScheduleParser().parse(schedule_workbook), sha256="retry-after"
    )
    assert result.import_id is not None
    repository.enqueue_notifications(
        [
            {
                "import_id": result.import_id,
                "destination_id": 123,
                "destination_kind": "user",
                "chat_type": "private",
                "chunk_index": 0,
                "text": "rate limited",
            }
        ]
    )
    bot = RetryOnceBot()
    notifier = ScheduleNotifier(  # type: ignore[arg-type]
        bot,
        repository,
        (),
        broadcast_interval=0,
        private_interval=0,
        group_interval=0,
    )

    started = monotonic()
    assert await notifier.deliver_pending() == 1

    assert monotonic() - started >= 0.9
    assert bot.attempts == 2
    assert repository.notification_outbox_counts() == {"sent": 1}
