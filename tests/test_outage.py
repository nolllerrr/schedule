from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.usefixtures("text_schedule_media")
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.methods import SendMessage

from schedule_bot.domain import Lesson, ParsedSchedule
from schedule_bot.downloader import ScheduleDownloadError
from schedule_bot.maintenance import backup_before_migration
from schedule_bot.notifier import ScheduleNotifier
from schedule_bot.outage import ScheduleOutageTracker
from schedule_bot.repository import ScheduleRepository


SOURCE = "https://ptgh.onego.ru/9006/"


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 14, 15, 30, tzinfo=timezone.utc)

    def advance(self, minutes: int = 30) -> None:
        self.now += timedelta(minutes=minutes)


class Bot:
    def __init__(self, failures: dict[int, list[Exception]] | None = None) -> None:
        self.failures = failures or {}
        self.sent: list[tuple[int, str]] = []
        self.calls = 0

    async def send_message(self, chat_id: int, text: str, **kwargs) -> SimpleNamespace:
        self.calls += 1
        errors = self.failures.get(chat_id, [])
        if errors:
            raise errors.pop(0)
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=self.calls)


def components(
    tmp_path: Path,
    *,
    clock: Clock | None = None,
    bot: Bot | None = None,
    admins: tuple[int, ...] = (101,),
    threshold: int = 3,
    source: str = SOURCE,
) -> tuple[ScheduleRepository, ScheduleNotifier, ScheduleOutageTracker, Clock, Bot]:
    clock = clock or Clock()
    bot = bot or Bot()
    repository = ScheduleRepository(tmp_path / "schedule.sqlite")
    notifier = ScheduleNotifier(
        bot,
        repository,
        admins,
        broadcast_interval=0,
        private_interval=0,
        group_interval=0,
        clock=lambda: clock.now,
    )
    tracker = ScheduleOutageTracker(
        repository,
        notifier,
        source,
        failure_threshold=threshold,
        timezone="Europe/Moscow",
        clock=lambda: clock.now,
    )
    return repository, notifier, tracker, clock, bot


def download_error(category: str = "http_502") -> ScheduleDownloadError:
    return ScheduleDownloadError("safe error", transient=True, category=category)


def admin_rows(repository: ScheduleRepository) -> list[dict[str, object]]:
    with sqlite3.connect(repository.database_path) as connection:
        connection.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM admin_notification_outbox ORDER BY id"
            )
        ]


@pytest.mark.asyncio
async def test_complete_lifecycle_and_one_warning_for_mixed_failures(
    tmp_path: Path,
) -> None:
    repository, _, tracker, clock, bot = components(tmp_path)
    assert tracker.get_state()["status"] == "HEALTHY"
    await tracker.record_success()
    clock.advance()
    state = await tracker.record_failure(download_error("ConnectTimeout"))
    assert state["status"] == "DEGRADED"
    assert state["consecutive_failures"] == 1
    first_failure = state["first_failure_at"]
    clock.advance()
    await tracker.record_failure(download_error("http_500"))
    assert bot.sent == []
    clock.advance()
    await tracker.record_failure(download_error())
    state = tracker.get_state()
    assert state["status"] == "DOWN"
    assert state["first_failure_at"] == first_failure
    assert state["warning_sent"] is True
    assert len(bot.sent) == 1
    warning = bot.sent[0][1]
    assert "Источник: ptgh.onego.ru" in warning
    assert "HTTP 502 Bad Gateway" in warning
    assert "14.10.2026 18:30" in warning
    assert "Неудачных проверок подряд: 3" in warning
    assert "Расписание ещё не было загружено" in warning
    assert "Ранее загруженное расписание сохранено" not in warning
    for _ in range(10):
        clock.advance()
        await tracker.record_failure(download_error("ConnectTimeout"))
    assert len(bot.sent) == 1
    assert tracker.get_state()["consecutive_failures"] == 13
    await tracker.record_success()
    state = tracker.get_state()
    assert state["status"] == "HEALTHY"
    assert state["consecutive_failures"] == 0
    assert len(bot.sent) == 2
    assert "Доступ к расписанию восстановлен" in bot.sent[1][1]
    assert "Продолжительность сбоя: 6 ч 0 мин" in bot.sent[1][1]
    await tracker.record_success()
    assert len(bot.sent) == 2
    assert [(r["kind"], r["status"]) for r in admin_rows(repository)] == [
        ("warning", "sent"),
        ("recovery", "sent"),
    ]


@pytest.mark.asyncio
async def test_degraded_success_has_no_recovery_then_new_independent_incident(
    tmp_path: Path,
) -> None:
    _, _, tracker, clock, bot = components(tmp_path)
    await tracker.record_failure(download_error())
    old_id = tracker.get_state()["incident_id"]
    await tracker.record_success()
    assert tracker.get_state()["status"] == "HEALTHY"
    assert bot.sent == []
    clock.advance()
    for _ in range(3):
        await tracker.record_failure(download_error())
    assert tracker.get_state()["incident_id"] != old_id
    assert len(bot.sent) == 1
    await tracker.record_success()
    for _ in range(3):
        await tracker.record_failure(download_error())
    assert len(bot.sent) == 3


@pytest.mark.asyncio
async def test_reopen_continues_down_without_duplicate_warning_and_preserves_timestamps(
    tmp_path: Path,
) -> None:
    _, _, tracker, clock, bot = components(tmp_path)
    await tracker.record_success()
    for _ in range(3):
        clock.advance()
        await tracker.record_failure(download_error())
    state = tracker.get_state()
    _, _, restarted, _, _ = components(tmp_path, clock=clock, bot=bot)
    assert restarted.get_state() == state
    clock.advance()
    await restarted.record_failure(download_error("ConnectTimeout"))
    assert len(bot.sent) == 1
    assert restarted.get_state()["incident_id"] == state["incident_id"]
    assert restarted.get_state()["first_failure_at"] == state["first_failure_at"]
    await restarted.record_success()
    assert len(bot.sent) == 2


@pytest.mark.asyncio
async def test_pending_warning_survives_restart_with_due_timer_and_bounded_backoff(
    tmp_path: Path,
) -> None:
    clock = Clock()
    bot = Bot(
        {
            101: [
                RuntimeError("https://user:secret@proxy.test/?key=value")
                for _ in range(11)
            ]
        }
    )
    repository, notifier, tracker, _, _ = components(
        tmp_path, clock=clock, bot=bot, threshold=1
    )
    await tracker.record_failure(download_error())
    assert bot.calls == 1 and bot.sent == []
    row = admin_rows(repository)[0]
    assert row["status"] == "pending"
    assert row["attempts"] == 1
    assert "secret" not in row["last_error"] and "value" not in row["last_error"]
    assert await notifier.deliver_admin_pending() == 0
    assert bot.calls == 1
    for attempt in range(1, 12):
        row = admin_rows(repository)[0]
        due = datetime.fromisoformat(row["next_attempt_at"])
        assert 1 <= (due - clock.now).total_seconds() <= 3600
        clock.now = due
        _, notifier, _, _, _ = components(tmp_path, clock=clock, bot=bot, threshold=1)
        # The ordinary worker entry point retries administrators independently of imports.
        sent = await notifier.deliver_pending()
        assert sent == (1 if attempt == 11 else 0)
    assert len(bot.sent) == 1
    assert admin_rows(repository)[0]["status"] == "sent"


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [TelegramBadRequest, TelegramForbiddenError])
async def test_permanent_rejection_does_not_retry_or_send_recovery(
    tmp_path: Path, error_type
) -> None:
    rejection = error_type(
        method=SendMessage(chat_id=101, text="alert"), message="rejected"
    )
    bot = Bot({101: [rejection]})
    repository, notifier, tracker, clock, _ = components(tmp_path, bot=bot, threshold=1)
    await tracker.record_failure(download_error())
    clock.advance(120)
    await notifier.deliver_pending()
    await tracker.record_success()
    assert bot.calls == 1
    assert bot.sent == []
    assert [(r["kind"], r["status"]) for r in admin_rows(repository)] == [
        ("warning", "failed")
    ]


@pytest.mark.asyncio
async def test_retry_after_persists_capped_due_time(tmp_path: Path) -> None:
    error = TelegramRetryAfter(
        method=SendMessage(chat_id=101, text="alert"), message="flood", retry_after=9000
    )
    repository, _, tracker, clock, bot = components(
        tmp_path, bot=Bot({101: [error]}), threshold=1
    )
    await tracker.record_failure(download_error())
    assert bot.calls == 1
    row = admin_rows(repository)[0]
    assert (
        datetime.fromisoformat(row["next_attempt_at"]) - clock.now
    ).total_seconds() == 3600


@pytest.mark.asyncio
async def test_recovery_only_for_admin_who_received_warning_and_cancels_unsent_warning(
    tmp_path: Path,
) -> None:
    bot = Bot({102: [RuntimeError("network")]})
    repository, notifier, tracker, clock, _ = components(
        tmp_path, bot=bot, admins=(101, 102), threshold=1
    )
    await tracker.record_failure(download_error())
    assert [chat for chat, _ in bot.sent] == [101]
    await tracker.record_success()
    assert [chat for chat, _ in bot.sent] == [101, 101]
    assert [
        (r["kind"], r["destination_id"], r["status"]) for r in admin_rows(repository)
    ] == [
        ("warning", 101, "sent"),
        ("warning", 102, "canceled"),
        ("recovery", 101, "sent"),
    ]
    clock.advance(180)
    await notifier.deliver_admin_pending()
    assert len(bot.sent) == 2


@pytest.mark.asyncio
async def test_no_recovery_when_source_recovers_before_warning_delivery(
    tmp_path: Path,
) -> None:
    repository, notifier, tracker, _, bot = components(tmp_path, threshold=1)
    notifier._worker_running = True
    await tracker.record_failure(download_error())
    assert tracker.get_state()["status"] == "DOWN"
    assert not tracker.get_state()["warning_sent"]
    await tracker.record_success()
    assert await notifier.deliver_admin_pending() == 0
    assert bot.sent == []
    assert admin_rows(repository)[0]["status"] == "canceled"


@pytest.mark.asyncio
async def test_concurrent_trackers_and_notifiers_send_one_warning(
    tmp_path: Path,
) -> None:
    clock, bot = Clock(), Bot()
    repository, notifier, tracker, _, _ = components(tmp_path, clock=clock, bot=bot)
    _, second_notifier, second, _, _ = components(tmp_path, clock=clock, bot=bot)
    await asyncio.gather(
        *(t.record_failure(download_error()) for t in [tracker, second] * 6)
    )
    assert tracker.get_state()["consecutive_failures"] == 12
    assert tracker.get_state()["warning_sent"] is True
    assert len(bot.sent) == 1
    await asyncio.gather(
        tracker.record_success(),
        second.record_success(),
        notifier.deliver_admin_pending(),
        second_notifier.deliver_admin_pending(),
    )
    assert len(bot.sent) == 2
    assert len(admin_rows(repository)) == 2


@pytest.mark.asyncio
async def test_recovery_waits_for_inflight_warning_and_acknowledgment(
    tmp_path: Path,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class WaitingBot(Bot):
        async def send_message(self, chat_id: int, text: str, **kwargs) -> SimpleNamespace:
            if "⚠️" in text:
                entered.set()
                await release.wait()
            return await super().send_message(chat_id, text)

    repository, _, tracker, _, bot = components(tmp_path, bot=WaitingBot(), threshold=1)
    failure = asyncio.create_task(tracker.record_failure(download_error()))
    await asyncio.wait_for(entered.wait(), 2)
    recovery = asyncio.create_task(tracker.record_success())
    await asyncio.sleep(0)
    assert not recovery.done()
    release.set()
    await asyncio.wait_for(asyncio.gather(failure, recovery), 3)
    assert ["⚠️" in text for _, text in bot.sent] == [True, False]
    assert [(r["kind"], r["status"]) for r in admin_rows(repository)] == [
        ("warning", "sent"),
        ("recovery", "sent"),
    ]


@pytest.mark.asyncio
async def test_warning_rechecks_cancellation_after_rate_limit_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    repository, notifier, tracker, _, bot = components(tmp_path, threshold=1)
    committed = asyncio.Event()
    loop = asyncio.get_running_loop()
    record_success = repository.record_source_success

    def observed_success(*args, **kwargs):
        state = record_success(*args, **kwargs)
        loop.call_soon_threadsafe(committed.set)
        return state

    monkeypatch.setattr(repository, "record_source_success", observed_success)

    async def wait_slot(*args) -> None:
        entered.set()
        await release.wait()

    notifier._wait_send_slot = wait_slot
    warning = asyncio.create_task(tracker.record_failure(download_error()))
    await asyncio.wait_for(entered.wait(), 2)
    # The active delivery lock does not prevent the success transaction canceling a wait.
    recovery = asyncio.create_task(tracker.record_success())
    try:
        await asyncio.wait_for(committed.wait(), 3)
        assert admin_rows(repository)[0]["status"] == "canceled"
        release.set()
        await asyncio.wait_for(asyncio.gather(warning, recovery), 3)
    finally:
        release.set()
        warning.cancel()
        recovery.cancel()
        await asyncio.gather(warning, recovery, return_exceptions=True)
    assert bot.sent == []


@pytest.mark.asyncio
async def test_warning_messages_and_database_never_contain_source_credentials(
    tmp_path: Path,
) -> None:
    source = "https://user:private-password@ptgh.onego.ru/9006/?token=private-query#private-fragment"
    repository, _, tracker, _, bot = components(tmp_path, source=source, threshold=1)
    await tracker.record_failure(
        ScheduleDownloadError(
            "private-password private-query", transient=True, category="ConnectTimeout"
        )
    )
    assert "ConnectTimeout" in bot.sent[0][1]
    text = bot.sent[0][1] + str(tracker.get_state()) + str(admin_rows(repository))
    assert "private-password" not in text
    assert "private-query" not in text
    assert "private-fragment" not in text


@pytest.mark.asyncio
async def test_critical_alert_retry_and_database_failure_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = Bot({101: [RuntimeError("network")]})
    repository, notifier, _, clock, _ = components(tmp_path, bot=bot)
    await notifier.notify_admins_error(
        RuntimeError(
            "https://user:private-password@server.test/?token=private-query <unsafe>"
        )
    )
    row = admin_rows(repository)[0]
    assert row["kind"] == "critical" and row["status"] == "pending"
    assert "private-password" not in row["text"] and "private-query" not in row["text"]
    assert "&lt;unsafe&gt;" in row["text"]
    clock.now = datetime.fromisoformat(row["next_attempt_at"])
    assert await notifier.deliver_pending() == 1
    assert len(bot.sent) == 1

    def broken(*args, **kwargs) -> None:
        raise sqlite3.OperationalError("unwritable database")

    monkeypatch.setattr(repository, "enqueue_admin_error", broken)
    await notifier.notify_admins_error(sqlite3.OperationalError("critical DB error"))
    assert len(bot.sent) == 2
    assert "critical DB error" in bot.sent[1][1]


@pytest.mark.asyncio
async def test_critical_alert_bypasses_backoff_warning_and_older_critical(
    tmp_path: Path,
) -> None:
    bot = Bot({101: [RuntimeError("network")]})
    repository, notifier, tracker, _, _ = components(tmp_path, bot=bot, threshold=1)
    await tracker.record_failure(download_error())
    assert bot.sent == []
    await notifier.notify_admins_error(
        sqlite3.IntegrityError("critical import failure")
    )
    assert len(bot.sent) == 1 and "critical import failure" in bot.sent[0][1]
    assert [(r["kind"], r["status"]) for r in admin_rows(repository)] == [
        ("warning", "pending"),
        ("critical", "sent"),
    ]

    bot.failures[101] = [RuntimeError("network")]
    await notifier.notify_admins_error(RuntimeError("older critical"))
    await notifier.notify_admins_error(RuntimeError("new critical"))
    assert len(bot.sent) == 2 and "new critical" in bot.sent[1][1]
    assert admin_rows(repository)[2]["status"] == "pending"


@pytest.mark.asyncio
async def test_actual_new_process_keeps_incident_and_sends_only_recovery(
    tmp_path: Path,
) -> None:
    repository, _, tracker, clock, bot = components(tmp_path)
    for _ in range(3):
        clock.advance()
        await tracker.record_failure(download_error())
    previous = tracker.get_state()
    assert len(bot.sent) == 1
    script = """
import asyncio, json, sys
from datetime import datetime
from types import SimpleNamespace
from schedule_bot.downloader import ScheduleDownloadError
from schedule_bot.notifier import ScheduleNotifier
from schedule_bot.outage import ScheduleOutageTracker
from schedule_bot.repository import ScheduleRepository
class Bot:
    def __init__(self): self.sent = []
    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append([chat_id, text])
        return SimpleNamespace(message_id=len(self.sent))
bot = Bot()
repository = ScheduleRepository(sys.argv[1])
clock = lambda: datetime.fromisoformat(sys.argv[2])
notifier = ScheduleNotifier(bot, repository, (101,), broadcast_interval=0, private_interval=0, group_interval=0, clock=clock)
tracker = ScheduleOutageTracker(repository, notifier, 'https://ptgh.onego.ru/9006/', clock=clock)
async def run():
    before = tracker.get_state()
    await tracker.record_failure(ScheduleDownloadError('safe', transient=True, category='ConnectTimeout'))
    failed = tracker.get_state()
    await tracker.record_success()
    print(json.dumps({'before': before, 'failed': failed, 'after': tracker.get_state(), 'sent': bot.sent}))
asyncio.run(run())
"""
    task_environment = dict(os.environ)
    task_environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    process = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            "-c",
            script,
            str(repository.database_path),
            clock.now.isoformat(),
        ],
        cwd=tmp_path,
        env=task_environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert process.returncode == 0, process.stderr
    result = json.loads(process.stdout)
    assert result["before"] == previous
    assert result["failed"]["incident_id"] == previous["incident_id"]
    assert result["failed"]["consecutive_failures"] == 4
    assert result["failed"]["warning_sent"] is True
    assert result["after"]["status"] == "HEALTHY"
    assert (
        len(result["sent"]) == 1
        and "Доступ к расписанию восстановлен" in result["sent"][0][1]
    )


@pytest.mark.asyncio
async def test_delivery_worker_retries_admin_queue_on_restart_without_source_update(
    tmp_path: Path,
) -> None:
    clock = Clock()
    repository, _, tracker, _, _ = components(
        tmp_path, clock=clock, bot=Bot({101: [RuntimeError("network")]}), threshold=1
    )
    await tracker.record_failure(download_error())
    clock.now = datetime.fromisoformat(admin_rows(repository)[0]["next_attempt_at"])
    delivered = asyncio.Event()

    class EventBot(Bot):
        async def send_message(self, chat_id: int, text: str, **kwargs) -> SimpleNamespace:
            result = await super().send_message(chat_id, text)
            delivered.set()
            return result

    _, notifier, _, _, bot = components(
        tmp_path, clock=clock, bot=EventBot(), threshold=1
    )
    worker = asyncio.create_task(notifier.run_delivery_worker(idle_seconds=0.01))
    try:
        await asyncio.wait_for(delivered.wait(), 2)
        for _ in range(100):
            if admin_rows(repository)[0]["status"] == "sent":
                break
            await asyncio.sleep(0.001)
        assert admin_rows(repository)[0]["status"] == "sent"
        assert len(bot.sent) == 1
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker


def test_migration_v8_preserves_import_user_outbox_and_pin_state(
    tmp_path: Path,
) -> None:
    legacy = ScheduleRepository(tmp_path / "schedule.sqlite")
    legacy.SCHEMA_VERSION = 7
    legacy.save_profile(101, role="student", target="ИС-22")
    legacy.save_chat_profile(
        -101,
        chat_type="supergroup",
        chat_title="Группа",
        target="ИС-22",
        configured_by=101,
    )
    legacy.set_chat_pin(-101, True)
    legacy.set_chat_last_message(-101, 55)
    day = date(2026, 10, 14)
    lesson = Lesson(day, "ИС-22", 1, "1 пара", 1, "09:00", "10:30", "Математика")
    # Seed the old schema directly: the current import API writes v9 columns.
    with sqlite3.connect(legacy.database_path) as connection:
        connection.execute("INSERT INTO imports(id,source_filename,sha256,imported_at,start_date,end_date) VALUES(1,'14.10.xlsx','old',?,?,?)", (day.isoformat(), day.isoformat(), day.isoformat()))
        connection.execute("INSERT INTO schedule_dates VALUES(1,?)", (day.isoformat(),))
        connection.execute("INSERT INTO lessons(import_id,is_current,lesson_date,group_name,position,lesson_label,lesson_number,start_time,end_time,subject,raw_json) VALUES(1,1,?,'ИС-22',1,'1 пара',1,'09:00','10:30',?,'{}')", (day.isoformat(), lesson.subject))
        connection.execute("INSERT INTO notification_outbox(import_id,destination_id,destination_kind,chat_type,chunk_index,text,created_at,next_attempt_at,expires_at) VALUES(1,101,'user','private',0,'Saved notification',?,?,?)", (day.isoformat(),day.isoformat(),day.isoformat()))
    tables = (
        "imports",
        "lessons",
        "notification_outbox",
        "chat_profiles",
        "user_profiles",
    )
    with sqlite3.connect(legacy.database_path) as connection:
        columns = {table: ",".join(row[1] for row in connection.execute(f"PRAGMA table_info({table})")) for table in tables}

    def snapshot() -> dict[str, list[tuple]]:
        with sqlite3.connect(legacy.database_path) as connection:
            return {
                table: connection.execute(
                    f"SELECT {columns[table]} FROM {table} ORDER BY rowid"
                ).fetchall()
                for table in tables
            }

    before = snapshot()
    assert legacy.current_schema_version() == 7
    latest = ScheduleRepository(legacy.database_path)
    backup = backup_before_migration(latest, tmp_path / "backups")
    assert backup is not None
    latest.initialize()
    assert latest.current_schema_version() == ScheduleRepository.SCHEMA_VERSION
    assert snapshot() == before
    assert latest.has_imported_schedule()
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7
        assert {
            table: connection.execute(
                f"SELECT {columns[table]} FROM {table} ORDER BY rowid"
            ).fetchall()
            for table in tables
        } == before


def test_failed_v8_migration_rolls_back_schema_and_preserves_backup(
    tmp_path, monkeypatch
):
    legacy = ScheduleRepository(tmp_path / "legacy.db")
    legacy.SCHEMA_VERSION = 7
    legacy.save_profile(123, role="student", target="ИС-22")
    latest = ScheduleRepository(legacy.database_path)
    backup = backup_before_migration(latest, tmp_path / "backups")

    def broken_migration(connection):
        ScheduleRepository._migrate_to_v8(connection)
        raise sqlite3.OperationalError("Simulated failure before schema commit")

    monkeypatch.setattr(latest, "_migrate_to_v8", broken_migration)
    with pytest.raises(sqlite3.OperationalError):
        latest.initialize()
    assert latest.current_schema_version() == 7
    for path in (legacy.database_path, backup):
        with sqlite3.connect(path) as connection:
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert (
                connection.execute("SELECT target FROM user_profiles").fetchone()[0]
                == "ИС-22"
            )
            assert not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'schedule_source_health'"
            ).fetchone()


@pytest.mark.asyncio
async def test_failure_and_warning_enqueue_rollback_together_on_sql_error(
    tmp_path: Path,
) -> None:
    repository, _, tracker, _, bot = components(tmp_path, threshold=1)
    repository.initialize()
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_admin_alert BEFORE INSERT ON admin_notification_outbox BEGIN SELECT RAISE(ABORT, 'failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        await tracker.record_failure(download_error())
    assert tracker.get_state()["status"] == "HEALTHY"
    assert bot.sent == []
    with sqlite3.connect(repository.database_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM schedule_outage_incidents"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.asyncio
async def test_sources_are_independent_and_existing_import_is_advertised(
    tmp_path: Path,
) -> None:
    repository, _, tracker, _, bot = components(tmp_path, threshold=1)
    day = date(2026, 10, 14)
    repository.import_schedule(
        ParsedSchedule("14.10.xlsx", day, day, (day,), ()), sha256="initial"
    )
    await tracker.record_failure(download_error())
    assert "Ранее загруженное расписание сохранено" in bot.sent[0][1]
    _, _, other_source, _, _ = components(
        tmp_path, source="https://another.test/schedule", threshold=1
    )
    assert other_source.get_state()["status"] == "HEALTHY"
    assert tracker.get_state()["status"] == "DOWN"


@pytest.mark.parametrize("threshold", [0, -1, 1.5])
def test_invalid_threshold_rejected(tmp_path: Path, threshold) -> None:
    with pytest.raises(ValueError):
        components(tmp_path, threshold=threshold)
