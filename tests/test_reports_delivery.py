import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendMessage

from schedule_bot.notifier import ScheduleNotifier
from schedule_bot.repository import ScheduleRepository
from schedule_bot.reports.models import ReportError
from test_reports import NOW, ready


@pytest.fixture
def repo(tmp_path):
    repository = ScheduleRepository(tmp_path / "reports.db")
    repository.initialize()
    return repository


def saved(repo):
    draft = ready(repo)
    return repo.report_submit(draft["id"], 100, draft["revision"], (900, 901), now=NOW)


def admin_draft(repo, report, admin, action):
    repo.report_admin_begin(report["id"], admin, action, (900, 901), now=NOW)
    return repo.report_admin_text(admin, "Проверил исходный файл", (900, 901), now=NOW)


@pytest.mark.parametrize("action", ["resolved", "rejected"])
def test_admin_close_is_atomic_idempotent_and_terminal(repo, action):
    report = saved(repo)
    assert repo.report_in_progress(report["id"], 900, (900,), now=NOW)["status"] == "in_progress"
    draft = admin_draft(repo, report, 900, action)
    first = repo.report_admin_confirm(draft["id"], 900, draft["revision"], (900,), now=NOW)
    assert first["status"] == action
    assert repo.report_admin_confirm(draft["id"], 900, draft["revision"], (900,), now=NOW)["status"] == action
    assert repo.report_in_progress(report["id"], 900, (900,), now=NOW)["status"] == action
    with repo._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM report_outbox WHERE kind='closed'").fetchone()[0] == 1


def test_reply_keeps_status_and_destination_without_admin_identity(repo):
    report = saved(repo)
    draft = admin_draft(repo, report, 900, "reply")
    assert repo.report_admin_confirm(draft["id"], 900, draft["revision"], (900,), now=NOW)["status"] == "new"
    with repo._connect() as connection:
        message = connection.execute("SELECT * FROM report_outbox WHERE kind='reply'").fetchone()
        event = connection.execute("SELECT * FROM report_events WHERE event_type='reply'").fetchone()
    assert message["destination_id"] == 100 and "900" not in message["payload"]
    assert event["text"] == "Проверил исходный файл"


def test_two_admins_close_only_once(repo):
    from concurrent.futures import ThreadPoolExecutor
    report = saved(repo)
    drafts = [admin_draft(repo, report, admin, action) for admin, action in [(900, "resolved"), (901, "rejected")]]
    def close(draft):
        return repo.report_admin_confirm(draft["id"], draft["admin_id"], draft["revision"], (900, 901), now=NOW)["status"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert len(set(pool.map(close, drafts))) == 1
    with repo._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM report_outbox WHERE kind='closed'").fetchone()[0] == 1


def test_every_admin_operation_checks_authorization(repo):
    report = saved(repo)
    draft = admin_draft(repo, report, 900, "resolved")
    actions = [
        lambda: repo.report_in_progress(report["id"], 100, (900,), now=NOW),
        lambda: repo.report_admin_begin(report["id"], 100, "reply", (900,), now=NOW),
        lambda: repo.report_admin_text(900, "Новое сообщение", (), now=NOW),
        lambda: repo.report_admin_confirm(draft["id"], 100, draft["revision"], (900,), now=NOW),
        lambda: repo.report_admin_begin(report["id"], 900, "new", (900,), now=NOW),
    ]
    for action in actions:
        with pytest.raises(ReportError):
            action()


def notifier(repo, bot=None, clock=None):
    return ScheduleNotifier(bot or SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=77))), repo,
                            (900, 901), clock=clock or (lambda: NOW), broadcast_interval=0, private_interval=0)


@pytest.mark.asyncio
async def test_delivery_and_success_evidence(repo):
    saved(repo)
    worker = notifier(repo)
    assert await worker.report_delivery.deliver_pending() == 2
    assert await worker.report_delivery.deliver_pending() == 0
    assert worker.bot.send_message.await_count == 2
    with repo._connect() as connection:
        rows = connection.execute("SELECT * FROM report_outbox").fetchall()
        outcomes = [row[0] for row in connection.execute("SELECT outcome FROM report_delivery_attempts")]
    assert all(row["status"] == "sent" and row["telegram_message_id"] == 77 for row in rows)
    assert outcomes == ["accepted", "accepted"]


@pytest.mark.asyncio
async def test_transient_failure_retry_and_no_text_in_logs(repo, caplog):
    saved(repo)
    clock = [NOW]
    secret = "PRIVATE_COMPLAINT_REPORT_TOKEN"
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=TelegramNetworkError(method=SendMessage(chat_id=900, text=secret), message=secret)))
    worker = notifier(repo, bot, lambda: clock[0])
    assert await worker.report_delivery.deliver_pending() == 0
    assert secret not in caplog.text
    with repo._connect() as connection:
        assert {row[0] for row in connection.execute("SELECT outcome FROM report_delivery_attempts")} == {"uncertain"}
        assert secret not in str([dict(row) for row in connection.execute("SELECT * FROM report_delivery_attempts")])
    bot.send_message.side_effect = None
    bot.send_message.return_value = SimpleNamespace(message_id=88)
    clock[0] += timedelta(seconds=30)
    restarted = notifier(ScheduleRepository(repo.database_path), bot, lambda: clock[0])
    assert await restarted.report_delivery.deliver_pending() == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("permanent", [True, False])
async def test_forbidden_and_retry_after(repo, permanent):
    saved(repo)
    error = (TelegramForbiddenError(method=SendMessage(chat_id=900, text="test"), message="blocked") if permanent
             else TelegramRetryAfter(method=SendMessage(chat_id=900, text="test"), message="flood", retry_after=120))
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=error))
    worker = notifier(repo, bot)
    await worker.report_delivery.deliver_pending()
    with repo._connect() as connection:
        row = connection.execute("SELECT * FROM report_outbox ORDER BY id LIMIT 1").fetchone()
    assert row["status"] == ("failed" if permanent else "pending")
    if not permanent:
        assert row["next_attempt_at"] == (NOW + timedelta(seconds=120)).isoformat()
    calls = bot.send_message.await_count
    if permanent:
        await worker.report_delivery.deliver_pending()
        assert bot.send_message.await_count == calls
    else:
        assert worker._pause_until > 0


@pytest.mark.asyncio
async def test_two_workers_cannot_send_the_same_row(repo):
    saved(repo)
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=66)))
    first, second = notifier(repo, bot), notifier(repo, bot)
    results = await asyncio.gather(first.report_delivery.deliver_pending(), second.report_delivery.deliver_pending())
    assert sum(results) == 2 and bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_cancelled_send_is_persisted_uncertain_and_retried(repo):
    saved(repo)
    entered = asyncio.Event()
    async def send(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=send))
    worker = notifier(repo, bot)
    task = asyncio.create_task(worker.report_delivery.deliver_pending())
    await asyncio.wait_for(entered.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with repo._connect() as connection:
        assert connection.execute("SELECT outcome FROM report_delivery_attempts").fetchone()[0] == "uncertain"
        assert connection.execute("SELECT status FROM report_outbox ORDER BY id").fetchone()[0] == "pending"


@pytest.mark.asyncio
async def test_delete_waits_for_admin_delivery_then_purges_fetched_candidates(repo):
    saved(repo)
    entered, release = asyncio.Event(), asyncio.Event()
    async def send(*args, **kwargs):
        entered.set()
        await release.wait()
        return SimpleNamespace(message_id=1)
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=send))
    worker = notifier(repo, bot)
    sending = asyncio.create_task(worker.report_delivery.deliver_pending())
    await asyncio.wait_for(entered.wait(), 3)
    deleting = asyncio.create_task(worker.delete_user_data(100, "synthetic"))
    await asyncio.sleep(0.05)
    assert not deleting.done()
    release.set()
    assert await deleting
    await sending
    with repo._connect() as connection:
        for table in ("reports", "report_events", "report_outbox", "report_drafts", "report_contexts", "report_delivery_attempts"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert bot.send_message.await_count == 1


def test_expired_lease_recovery_keeps_uncertain_evidence(repo):
    saved(repo)
    candidate = repo.report_pending(now=NOW)[0]
    claimed = repo.report_claim(candidate["id"], now=NOW)
    assert repo.report_claim(candidate["id"], now=NOW) is None
    assert repo.report_claim(candidate["id"], now=NOW + timedelta(minutes=3))["lease_token"] != claimed["lease_token"]
    with repo._connect() as connection:
        assert connection.execute("SELECT outcome FROM report_delivery_attempts ORDER BY id").fetchone()[0] == "uncertain"


def test_closed_retention_and_open_report_survival(repo):
    report = saved(repo)
    draft = admin_draft(repo, report, 900, "resolved")
    repo.report_admin_confirm(draft["id"], 900, draft["revision"], (900,), now=NOW)
    open_draft = ready(repo, user=101)
    opened = repo.report_submit(open_draft["id"], 101, open_draft["revision"], (900,), now=NOW)
    assert repo.report_cleanup(now=NOW + timedelta(days=91), dry_run=True)["reports"] == 1
    assert repo.report_get(report["id"])
    repo.report_cleanup(now=NOW + timedelta(days=91))
    assert repo.report_get(report["id"]) is None
    assert repo.report_get(opened["id"])


def test_plain_text_html_is_preserved_as_data(repo):
    draft = ready(repo, description='<b>Аудитория</b> <script>alert("x")</script> & 47')
    report = repo.report_submit(draft["id"], 100, draft["revision"], (900,), now=NOW)
    with repo._connect() as connection:
        payload = json.loads(connection.execute("SELECT payload FROM report_outbox").fetchone()[0])
    assert report["description"] in payload["text"]
