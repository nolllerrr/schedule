from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from schedule_bot.domain import Lesson, ParsedSchedule
from schedule_bot.maintenance import backup_before_migration
from schedule_bot.repository import ScheduleRepository


NOW = datetime(2026, 10, 13, 10, tzinfo=timezone.utc)
DAY = date(2026, 10, 14)


def seed(repository: ScheduleRepository, *, chunks: int = 3) -> int:
    lesson = Lesson(DAY, "ИС-22", 1, "1 пара", 1, "09:00", "10:30", "Математика")
    parsed = ParsedSchedule("14.10.xlsx", DAY, DAY, (DAY,), (lesson,))
    result = repository.import_schedule(parsed, sha256="original")
    assert result.import_id is not None
    repository.save_profile(123, role="student", target="ИС-22")
    repository.enqueue_notifications(
        ({
            "import_id": result.import_id, "destination_id": 123,
            "destination_kind": "user", "chat_type": "private",
            "chunk_index": index, "text": f"part {index}",
        }
        for index in range(chunks)), now=NOW,
    )
    return result.import_id


def rows(repository: ScheduleRepository) -> list[dict]:
    with sqlite3.connect(repository.database_path) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute("SELECT * FROM notification_outbox ORDER BY id")]


def test_retry_survives_restart_and_more_than_five_attempts(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "db.sqlite"
    repository = ScheduleRepository(path)
    seed(repository)
    monkeypatch.setattr("schedule_bot.repository.random.uniform", lambda low, high: 1.0)
    now = NOW
    for attempt in range(1, 11):
        delivery = repository.pending_notifications(now=now)[0]
        assert delivery["chunk_index"] == 0
        repository.mark_notification_failed(int(delivery["id"]), "network", now=now)
        repository = ScheduleRepository(path)
        head = rows(repository)[0]
        assert head["status"] == "pending"
        assert head["attempts"] == attempt
        due = datetime.fromisoformat(head["next_attempt_at"])
        assert (due - now).total_seconds() == min(15 * 2 ** (attempt - 1), 3600)
        assert repository.pending_notifications(now=due - timedelta(microseconds=1)) == []
        now = due
    repository.mark_notification_sent(int(delivery["id"]))
    assert repository.pending_notifications(now=now)[0]["chunk_index"] == 1


def test_backoff_has_jitter_and_retry_after_is_respected(tmp_path: Path, monkeypatch) -> None:
    repository = ScheduleRepository(tmp_path / "db.sqlite")
    seed(repository)
    head = rows(repository)[0]
    monkeypatch.setattr("schedule_bot.repository.random.uniform", lambda low, high: 1.2)
    repository.mark_notification_failed(head["id"], "network", now=NOW)
    assert datetime.fromisoformat(rows(repository)[0]["next_attempt_at"]) == NOW + timedelta(seconds=18)
    repository.mark_notification_failed(head["id"], "rate limit", retry_after=7200, now=NOW)
    assert repository.pending_notifications(now=NOW + timedelta(seconds=7199)) == []
    assert repository.pending_notifications(now=NOW + timedelta(seconds=7200))[0]["id"] == head["id"]


def test_permanent_head_failure_blocks_tail_and_redrive_restores_order(tmp_path: Path) -> None:
    repository = ScheduleRepository(tmp_path / "db.sqlite")
    import_id = seed(repository)
    first, second, third = rows(repository)
    assert [item["id"] for item in repository.pending_notifications(now=NOW)] == [first["id"]]
    repository.mark_notification_sent(first["id"])
    repository.mark_notification_failed(second["id"], "BadRequest", permanent=True, now=NOW)
    assert repository.pending_notifications(now=NOW) == []
    assert [(r["status"], r["failure_kind"]) for r in rows(repository)] == [
        ("sent", None), ("failed", "permanent"), ("failed", "blocked"),
    ]
    assert repository.redrive_notifications(import_id, 123, "user", now=NOW) == 2
    assert [item["id"] for item in repository.pending_notifications(now=NOW)] == [second["id"]]
    assert rows(repository)[0]["status"] == "sent"
    assert rows(repository)[1]["attempts"] == 0
    assert not repository.notification_is_sendable(third["id"], now=NOW)
    repository.mark_notification_sent(second["id"])
    assert repository.notification_is_sendable(third["id"], now=NOW)


def test_expiry_terminates_unsent_series_and_manual_retry_renews_it(tmp_path: Path) -> None:
    repository = ScheduleRepository(tmp_path / "db.sqlite")
    import_id = seed(repository)
    first = rows(repository)[0]
    repository.mark_notification_sent(first["id"])
    expired = NOW + ScheduleRepository.NOTIFICATION_TTL
    assert repository.pending_notifications(now=expired) == []
    assert [r["status"] for r in rows(repository)] == ["sent", "failed", "failed"]
    assert [r["failure_kind"] for r in rows(repository)][1:] == ["expired", "expired"]
    assert repository.redrive_notifications(import_id, 123, "user", now=expired) == 2
    assert repository.pending_notifications(now=expired)[0]["chunk_index"] == 1
    assert datetime.fromisoformat(rows(repository)[1]["expires_at"]) == expired + ScheduleRepository.NOTIFICATION_TTL


@pytest.mark.parametrize("action", ["disable", "delete"])
def test_redrive_requires_active_subscription(tmp_path: Path, action: str) -> None:
    repository = ScheduleRepository(tmp_path / "db.sqlite")
    import_id = seed(repository)
    if action == "disable":
        repository.set_notifications(123, False)
        assert not repository.notification_is_sendable(rows(repository)[0]["id"], now=NOW)
        assert {r["failure_kind"] for r in rows(repository)} == {"cancelled"}
    else:
        repository.delete_user_data(123, "actor")
    with pytest.raises(ValueError, match="active notification subscription"):
        repository.redrive_notifications(import_id, 123, "user", now=NOW)


def test_delete_purges_all_personal_statuses_and_preserves_common_data(tmp_path: Path) -> None:
    repository = ScheduleRepository(tmp_path / "db.sqlite")
    import_id = seed(repository)
    first, second, _ = rows(repository)
    repository.mark_notification_sent(first["id"])
    repository.mark_notification_failed(second["id"], "permanent", permanent=True, now=NOW)
    repository.enqueue_notifications([{
        "import_id": import_id, "destination_id": 123, "destination_kind": "user",
        "chat_type": "private", "chunk_index": 3, "text": "queued later",
    }], now=NOW)
    assert {r["status"] for r in rows(repository)} == {"pending", "sent", "failed"}
    repository.save_chat_profile(-100, chat_type="group", chat_title="ИС-22", target="ИС-22", configured_by=123)
    repository.enqueue_notifications([{
        "import_id": import_id, "destination_id": -100, "destination_kind": "chat",
        "chat_type": "group", "chunk_index": 0, "text": "common",
    }], now=NOW)
    repository.record_usage_event("actor", chat_type="private", event_type="notification_sent")
    repository.record_usage_event("other", chat_type="private", event_type="bot_started")
    assert repository.delete_user_data(123, "actor")
    assert repository.get_profile(123) is None
    assert [(r["destination_id"], r["text"]) for r in rows(repository)] == [(-100, "common")]
    assert repository.get_chat_profile(-100)["configured_by"] == 0
    assert repository.lessons_for(role="student", target="ИС-22", lesson_date=DAY)
    with sqlite3.connect(repository.database_path) as connection:
        assert connection.execute("SELECT actor_key FROM usage_events").fetchall() == [("other",)]
        assert connection.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 1
    lesson = repository.lessons_for(role="student", target="ИС-22", lesson_date=DAY)[0]
    parsed = ParsedSchedule("14.10.xlsx", DAY, DAY, (DAY,), (lesson,))
    repository.import_schedule(replace(parsed, lessons=(replace(parsed.lessons[0], subject="Физика"),)), sha256="updated")
    assert all(r["destination_kind"] == "chat" for r in rows(repository))
    assert not repository.delete_user_data(123, "actor")


def test_delete_rolls_back_all_personal_changes_on_sql_failure(tmp_path: Path) -> None:
    repository = ScheduleRepository(tmp_path / "db.sqlite")
    seed(repository)
    repository.record_usage_event("actor", chat_type="private", event_type="bot_started")
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("CREATE TRIGGER fail_delete BEFORE DELETE ON notification_outbox BEGIN SELECT RAISE(ABORT, 'test failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="test failure"):
        repository.delete_user_data(123, "actor")
    assert repository.get_profile(123) is not None
    assert len(rows(repository)) == 3
    with sqlite3.connect(repository.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 1


@pytest.mark.parametrize("role,target", [("student", "ПД-12"), ("teacher", "Иванов И.И.")])
def test_profile_change_preserves_disabled_notifications(tmp_path: Path, role: str, target: str) -> None:
    repository = ScheduleRepository(tmp_path / "db.sqlite")
    repository.save_profile(123, role="student", target="ИС-22")
    assert repository.get_profile(123)["notifications"] is True
    repository.set_notifications(123, False)
    repository.save_profile(123, role=role, target=target)
    assert repository.get_profile(123) == {"user_id": 123, "role": role, "target": target, "notifications": False}
    repository.save_profile(123, role=role, target=target, notifications=True)
    assert repository.get_profile(123)["notifications"] is True
    repository.save_profile(456, role="student", target="ИС-22", notifications=False)
    assert repository.get_profile(456)["notifications"] is False


def test_v6_migration_preserves_pin_and_old_delivery_state_with_backup(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        for migration in (
            ScheduleRepository._migrate_to_v1, ScheduleRepository._migrate_to_v2,
            ScheduleRepository._migrate_to_v3, ScheduleRepository._migrate_to_v4,
            ScheduleRepository._migrate_to_v5, ScheduleRepository._migrate_to_v6,
        ):
            migration(connection)
        connection.execute("INSERT INTO imports(id,source_filename,sha256,imported_at,start_date,end_date) VALUES (1,'old.xlsx','old',?,?,?)", (NOW.isoformat(), str(DAY), str(DAY)))
        connection.execute("INSERT INTO user_profiles VALUES (123,'student','ИС-22',0,?)", (NOW.isoformat(),))
        connection.execute("INSERT INTO chat_profiles(chat_id,chat_type,target,configured_by,created_at,updated_at,pin_enabled,last_pinned_message_id,pending_pin_message_id) VALUES (-100,'group','ИС-22',123,?,?,1,10,11)", (NOW.isoformat(), NOW.isoformat()))
        for index, status in enumerate(("sent", "failed", "pending")):
            connection.execute("INSERT INTO notification_outbox(import_id,destination_id,destination_kind,chat_type,chunk_index,text,status,attempts,created_at) VALUES (1,123,'user','private',?,?,?,5,?)", (index, f"part {index}", status, NOW.isoformat()))
        connection.execute("INSERT INTO notification_outbox(import_id,destination_id,destination_kind,chat_type,chunk_index,text,attempts,created_at) VALUES (1,456,'user','private',0,'recoverable',5,?)", (NOW.isoformat(),))
        connection.execute("INSERT INTO notification_outbox(import_id,destination_id,destination_kind,chat_type,chunk_index,text,attempts,created_at) VALUES (1,789,'user','private',0,'expired',5,?)", ((NOW - timedelta(days=8)).isoformat(),))
        connection.execute("PRAGMA user_version = 6")
    repository = ScheduleRepository(path)
    backup = backup_before_migration(repository, tmp_path / "backups")
    assert backup is not None
    repository.initialize()
    assert repository.current_schema_version() == 7
    assert repository.get_profile(123)["notifications"] is False
    profile = repository.get_chat_profile(-100)
    assert (profile["last_pinned_message_id"], profile["pending_pin_message_id"]) == (10, 11)
    assert [(r["status"], r["failure_kind"]) for r in rows(repository)[:3]] == [("sent", None), ("failed", "legacy"), ("failed", "blocked")]
    assert all(r["attempts"] == 5 for r in rows(repository))
    assert [r["text"] for r in repository.pending_notifications(now=NOW)] == ["recoverable"]
    assert rows(repository)[4]["failure_kind"] == "expired"
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert connection.execute("SELECT status FROM notification_outbox WHERE destination_id = 123 ORDER BY id").fetchall() == [("sent",), ("failed",), ("pending",)]
