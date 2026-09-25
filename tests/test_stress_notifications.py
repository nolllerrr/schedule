"""Opt-in local load test: RUN_STRESS_TEST=1 STRESS_USERS=1000 pytest -s ..."""

from __future__ import annotations

import asyncio
import os
from collections import Counter
from datetime import date
from time import perf_counter
from types import SimpleNamespace

import pytest

from schedule_bot.domain import Lesson, ParsedSchedule
from schedule_bot.notifier import ScheduleNotifier
from schedule_bot.repository import ScheduleRepository


class CountingBot:
    def __init__(self, *, delay_seconds: float) -> None:
        self.delay_seconds = delay_seconds
        self.sent: Counter[int] = Counter()
        self.sent_dates: dict[int, Counter[str]] = {}
        self.total_sent = 0
        self.invalid_messages = 0

    async def send_message(self, chat_id: int, text: str) -> SimpleNamespace:
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        found_dates = [
            marker for marker in ("28 сентября", "29 сентября") if marker in text
        ]
        if len(found_dates) != 1:
            self.invalid_messages += 1
        else:
            self.sent_dates.setdefault(chat_id, Counter())[found_dates[0]] += 1
        self.sent[chat_id] += 1
        self.total_sent += 1
        return SimpleNamespace(message_id=self.total_sent)


@pytest.mark.skipif(
    os.environ.get("RUN_STRESS_TEST") != "1",
    reason="Run explicitly with RUN_STRESS_TEST=1",
)
@pytest.mark.asyncio
async def test_mass_notification_delivery(tmp_path) -> None:
    user_count = int(os.environ.get("STRESS_USERS", "1000"))
    delay_seconds = float(os.environ.get("STRESS_SEND_DELAY_MS", "0")) / 1000
    assert user_count > 0
    assert delay_seconds >= 0

    repository = ScheduleRepository(tmp_path / "stress.db")
    dates = (date(2026, 9, 28), date(2026, 9, 29))
    lessons = tuple(
        Lesson(
            lesson_date=lesson_date,
            group_name="ИС-22",
            position=1,
            lesson_label="1 пара",
            lesson_number=1,
            start_time="09:00",
            end_time="10:30",
            subject="Тестовая дисциплина",
            teacher="Тестовый преподаватель",
            room="46",
        )
        for lesson_date in dates
    )
    parsed = ParsedSchedule(
        source_filename="stress-test.xlsx",
        start_date=dates[0],
        end_date=dates[-1],
        dates=dates,
        lessons=lessons,
    )
    setup_start = perf_counter()
    for user_id in range(1, user_count + 1):
        repository.save_profile(user_id, role="student", target="ИС-22")
    setup_seconds = perf_counter() - setup_start

    result = repository.import_schedule(parsed, sha256="stress-test")
    bot = CountingBot(delay_seconds=delay_seconds)
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
        await asyncio.sleep(0)
        enqueue_start = perf_counter()
        await notifier.notify_import(result)
        enqueue_seconds = perf_counter() - enqueue_start
        after_enqueue = repository.notification_outbox_counts()

        drain_start = perf_counter()
        while repository.notification_outbox_counts().get("pending", 0):
            assert perf_counter() - drain_start < 600, "Delivery worker stalled"
            await asyncio.sleep(0.05)
        drain_seconds = perf_counter() - drain_start
        final = repository.notification_outbox_counts()
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker

    print(
        f"users={user_count} messages={user_count * len(dates)} "
        f"delay_ms={delay_seconds * 1000:g} "
        f"setup_s={setup_seconds:.2f} "
        f"enqueue_s={enqueue_seconds:.2f} "
        f"sent_after_enqueue={after_enqueue.get('sent', 0)} "
        f"pending_after_enqueue={after_enqueue.get('pending', 0)} "
        f"drain_s={drain_seconds:.2f} "
        f"total_delivery_s={enqueue_seconds + drain_seconds:.2f}"
    )
    assert final == {"sent": user_count * len(dates)}
    assert len(bot.sent) == user_count
    assert all(count == len(dates) for count in bot.sent.values())
    assert all(
        date_counts == {"28 сентября": 1, "29 сентября": 1}
        for date_counts in bot.sent_dates.values()
    )
    assert bot.invalid_messages == 0
