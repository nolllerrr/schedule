import asyncio
import os
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.usefixtures("text_schedule_media")
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import SendMessage

from schedule_bot.delivery_audit import (
    begin_attempt, record_attempt, review_attempt, unresolved_attempts,
)
from schedule_bot.domain import Lesson, ParsedSchedule
from schedule_bot.notifier import ScheduleNotifier
from schedule_bot.repository import ScheduleRepository


NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)


def queued(tmp_path, admin=False):
    repository = ScheduleRepository(tmp_path / "delivery.db")
    repository.initialize()
    if admin:
        repository.enqueue_admin_error("Synthetic alert", (123,), now=NOW)
    else:
        repository.save_profile(123, role="student", target="ИС-22")
        day = date(2026, 10, 17)
        lesson = Lesson(day, "ИС-22", 1, "1 пара", 1, "09:00", "10:30", "Synthetic lesson")
        repository.import_schedule(
            ParsedSchedule("17.10.2026.xlsx", day, day, (day,), (lesson,)), sha256="original",
        )
    return repository


def worker(repository, send):
    return ScheduleNotifier(
        SimpleNamespace(send_message=send), repository, (123,),
        clock=lambda: datetime.now(timezone.utc),
        broadcast_interval=0, private_interval=0, group_interval=0,
    )


def attempts(repository):
    with sqlite3.connect(repository.database_path) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute("SELECT * FROM delivery_attempts ORDER BY id")]


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [False, True])
async def test_attempt_is_durable_before_external_send_and_keeps_message_id(tmp_path, admin):
    repository = queued(tmp_path, admin)

    async def send(*args, **kwargs):
        rows = attempts(ScheduleRepository(repository.database_path))
        assert len(rows) == 1 and rows[0]["outcome"] == "uncertain"
        assert rows[0]["completed_at"] is None
        return SimpleNamespace(message_id=987)

    assert await worker(repository, send).deliver_pending() == 1
    row = attempts(repository)[0]
    assert row["outcome"] == "sent"
    assert row["telegram_message_id"] == 987
    assert row["completed_at"] is not None
    assert not unresolved_attempts(repository)


@pytest.mark.asyncio
async def test_failed_attempt_insert_prevents_external_send(tmp_path):
    repository = queued(tmp_path)
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_attempt BEFORE INSERT ON delivery_attempts "
            "BEGIN SELECT RAISE(ABORT,'synthetic audit failure'); END"
        )
    send = AsyncMock()
    with pytest.raises(sqlite3.IntegrityError):
        await worker(repository, send).deliver_pending()
    send.assert_not_awaited()
    assert repository.notification_outbox_counts() == {"pending": 1}


@pytest.mark.asyncio
async def test_sql_ack_failure_preserves_accepted_response_for_diagnosis(tmp_path, monkeypatch):
    repository = queued(tmp_path)

    def broken_ack(delivery_id):
        raise sqlite3.OperationalError("synthetic ack failure")

    monkeypatch.setattr(repository, "mark_notification_sent", broken_ack)
    send = AsyncMock(return_value=SimpleNamespace(message_id=456))
    with pytest.raises(sqlite3.OperationalError):
        await worker(repository, send).deliver_pending()
    row = unresolved_attempts(repository)[0]
    assert row["outcome"] == "accepted"
    assert row["telegram_message_id"] == 456
    assert row["error_category"] == "acknowledgment_failed"
    assert repository.notification_outbox_counts() == {"pending": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [False, True])
@pytest.mark.parametrize("category", ["network", "rejected", "rate_limit"])
async def test_send_failure_records_safe_outcome(tmp_path, admin, category, caplog):
    repository = queued(tmp_path, admin)
    error = RuntimeError("https://user:secret@example.test/?token=private-query")
    if category == "rejected":
        error = TelegramBadRequest(method=SendMessage(chat_id=123, text="private-text"), message="rejected")
    elif category == "rate_limit":
        error = TelegramRetryAfter(method=SendMessage(chat_id=123, text="private-text"), message="slow", retry_after=1)
    send = AsyncMock(side_effect=error)
    notifier = worker(repository, send)
    notifier._wait_send_slot = AsyncMock()
    assert await notifier.deliver_pending() == 0
    rows = attempts(repository)
    assert rows and all(row["outcome"] == ("uncertain" if category == "network" else "rejected") for row in rows)
    assert all(row["error_category"] == type(error).__name__ for row in rows)
    assert "private-text" not in caplog.text
    assert "private-query" not in caplog.text
    assert "secret" not in caplog.text


@pytest.mark.asyncio
async def test_cancellation_during_api_keeps_interrupted_attempt(tmp_path):
    repository = queued(tmp_path)
    entered = asyncio.Event()

    async def send(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(worker(repository, send).deliver_pending())
    await asyncio.wait_for(entered.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = unresolved_attempts(repository)[0]
    assert row["outcome"] == "uncertain"
    assert row["error_category"] == "interrupted"
    assert repository.notification_outbox_counts() == {"pending": 1}


@pytest.mark.asyncio
async def test_process_crash_exposes_at_least_once_duplicate_window(tmp_path):
    repository = queued(tmp_path)
    receipt = tmp_path / "synthetic_external_receipt.txt"
    code = (
        "import os,sys; from pathlib import Path; "
        "from schedule_bot.repository import ScheduleRepository; "
        "from schedule_bot.delivery_audit import begin_attempt; "
        "repo=ScheduleRepository(sys.argv[1]); begin_attempt(repo,notification_id=1); "
        "Path(sys.argv[2]).write_text('accepted\\n'); os._exit(47)"
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    process = await asyncio.to_thread(
        subprocess.run, [sys.executable, "-c", code, str(repository.database_path), str(receipt)],
        env=environment, capture_output=True, timeout=15,
    )
    assert process.returncode == 47
    assert len(unresolved_attempts(repository)) == 1

    async def retry(*args, **kwargs):
        with receipt.open("a") as target:
            target.write("accepted\n")
        return SimpleNamespace(message_id=222)

    assert await worker(repository, retry).deliver_pending() == 1
    assert len(receipt.read_text().splitlines()) == 2
    assert [row["outcome"] for row in attempts(repository)] == ["uncertain", "sent"]


def test_review_preserves_uncertainty_and_redacts_reason(tmp_path):
    repository = queued(tmp_path)
    attempt_id = begin_attempt(repository, notification_id=1)
    record_attempt(repository, attempt_id, "uncertain", error_category="timeout")
    assert review_attempt(repository, attempt_id, "Checked https://user:secret@example.test/?token=private-query")
    row = attempts(repository)[0]
    assert row["outcome"] == "uncertain" and row["reviewed_at"]
    assert "secret" not in row["review_reason"]
    assert "private-query" not in row["review_reason"]
    assert not unresolved_attempts(repository)
    assert repository.notification_outbox_counts() == {"pending": 1}
    assert not review_attempt(repository, attempt_id, "Second review")
    with pytest.raises(ValueError):
        review_attempt(repository, attempt_id, " ")


@pytest.mark.asyncio
async def test_personal_deletion_cascades_delivery_evidence(tmp_path):
    repository = queued(tmp_path)
    begin_attempt(repository, notification_id=1)
    notifier = worker(repository, AsyncMock())
    assert await notifier.delete_user_data(123, ("old-actor-key", "new-actor-key"))
    assert not attempts(repository)


def test_attempt_requires_single_reference_and_safe_categories(tmp_path):
    repository = queued(tmp_path)
    with pytest.raises(ValueError):
        begin_attempt(repository)
    with pytest.raises(ValueError):
        begin_attempt(repository, notification_id=1, admin_notification_id=1)
    attempt_id = begin_attempt(repository, notification_id=1)
    with pytest.raises(ValueError):
        record_attempt(repository, attempt_id, "uncertain", error_category="token=private")
    with pytest.raises(ValueError):
        unresolved_attempts(repository, limit=0)
