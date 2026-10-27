import asyncio
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_delivery_audit import attempts, queued, worker

from schedule_bot import notifier as notifier_module
from schedule_bot.repository import ScheduleRepository

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("text_schedule_media")]


@pytest.mark.parametrize("failed_outcome", ["accepted", "sent"])
async def test_journal_failure_preserves_evidence_and_recovery(tmp_path, monkeypatch, failed_outcome):
    repository = queued(tmp_path)
    real_record = notifier_module.record_attempt

    def record(repo, attempt_id, outcome, **kwargs):
        if outcome == failed_outcome:
            raise sqlite3.OperationalError("synthetic journal failure")
        return real_record(repo, attempt_id, outcome, **kwargs)

    monkeypatch.setattr(notifier_module, "record_attempt", record)
    send = AsyncMock(return_value=SimpleNamespace(message_id=456))
    with pytest.raises(sqlite3.OperationalError, match="journal failure"):
        await worker(repository, send).deliver_pending()
    row = attempts(repository)[0]
    assert row["outcome"] == ("uncertain" if failed_outcome == "accepted" else "accepted")
    assert row["telegram_message_id"] == (None if failed_outcome == "accepted" else 456)
    assert repository.notification_outbox_counts() == {("pending" if failed_outcome == "accepted" else "sent"): 1}
    monkeypatch.setattr(notifier_module, "record_attempt", real_record)
    retry_send = AsyncMock(return_value=SimpleNamespace(message_id=789))
    recovered = worker(ScheduleRepository(repository.database_path), retry_send)
    assert await recovered.deliver_pending() == (1 if failed_outcome == "accepted" else 0)
    assert retry_send.await_count == (1 if failed_outcome == "accepted" else 0)
    assert attempts(repository)[0]["outcome"] == ("uncertain" if failed_outcome == "accepted" else "accepted")


async def test_cancellation_waits_for_failure_journal_update(tmp_path, monkeypatch):
    repository = queued(tmp_path)
    entered, release = threading.Event(), threading.Event()
    real_record = notifier_module.record_attempt

    def record(repo, attempt_id, outcome, **kwargs):
        if kwargs.get("error_category") == "RuntimeError":
            entered.set()
            assert release.wait(5)
        return real_record(repo, attempt_id, outcome, **kwargs)

    monkeypatch.setattr(notifier_module, "record_attempt", record)
    send = AsyncMock(side_effect=RuntimeError("synthetic failure"))
    task = asyncio.create_task(worker(repository, send).deliver_pending())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    assert attempts(repository)[0]["outcome"] == "uncertain"
    assert attempts(repository)[0]["error_category"] == "RuntimeError"
    assert repository.notification_outbox_counts() == {"pending": 1}
