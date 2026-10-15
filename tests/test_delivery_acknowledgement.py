from __future__ import annotations

import asyncio
import sqlite3
import threading
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from schedule_bot.domain import Lesson, ParsedSchedule
from schedule_bot.notifier import ScheduleNotifier
from schedule_bot.repository import ScheduleRepository


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [False, True])
async def test_cancellation_finishes_sent_acknowledgment_before_unlock(
    tmp_path, monkeypatch, admin
):
    repository = ScheduleRepository(tmp_path / "ack.db")
    repository.initialize()
    if admin:
        repository.enqueue_admin_error("Critical alert", (123,))
    else:
        repository.save_profile(123, role="student", target="ИС-22")
        day = date(2026, 10, 14)
        lesson = Lesson(day, "ИС-22", 1, "1 пара", 1, "09:00", "10:30", "Математика")
        repository.import_schedule(
            ParsedSchedule("14.10.xlsx", day, day, (day,), (lesson,)), sha256="initial"
        )
    notifier = ScheduleNotifier(
        SimpleNamespace(send_message=AsyncMock()),
        repository,
        (123,),
        broadcast_interval=0,
        private_interval=0,
        group_interval=0,
    )
    method = "mark_admin_notification_sent" if admin else "mark_notification_sent"
    original = getattr(repository, method)
    entered, release = threading.Event(), threading.Event()

    def delayed_ack(*args, **kwargs):
        entered.set()
        assert release.wait(3), "Test did not release the SQLite acknowledgment"
        return original(*args, **kwargs)

    monkeypatch.setattr(repository, method, delayed_ack)
    delivery = asyncio.create_task(notifier.deliver_pending())
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        delivery.cancel()
        await asyncio.sleep(0)
        assert not delivery.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await delivery
    finally:
        release.set()
        if not delivery.done():
            delivery.cancel()
            with pytest.raises(asyncio.CancelledError):
                await delivery
    table = "admin_notification_outbox" if admin else "notification_outbox"
    with sqlite3.connect(repository.database_path) as connection:
        assert connection.execute(f"SELECT status FROM {table}").fetchone()[0] == "sent"
    assert await notifier.deliver_pending() == 0
    notifier.bot.send_message.assert_awaited_once()
