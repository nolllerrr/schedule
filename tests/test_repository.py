import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from schedule_bot.parser import ExcelScheduleParser
from schedule_bot.repository import ScheduleRepository


def test_initialize_creates_latest_schema_and_is_idempotent(tmp_path: Path) -> None:
    database_path = tmp_path / "schedule.db"
    repository = ScheduleRepository(database_path)

    repository.initialize()
    repository.initialize()

    with sqlite3.connect(database_path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert version == ScheduleRepository.SCHEMA_VERSION == 3
    assert journal_mode == "wal"
    assert {
        "user_profiles",
        "chat_profiles",
        "usage_events",
        "notification_outbox",
    } <= tables


def test_migrate_v1_preserves_existing_profile(tmp_path: Path) -> None:
    database_path = tmp_path / "schedule.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE user_profiles (
                user_id INTEGER PRIMARY KEY,
                role TEXT NOT NULL CHECK(role IN ('student', 'teacher')),
                target TEXT NOT NULL,
                notifications INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO user_profiles(user_id, role, target, notifications, updated_at)
            VALUES (123, 'student', 'ПД-12', 0, '2026-09-23T10:00:00+00:00')
            """
        )
        connection.execute("PRAGMA user_version = 1")
        connection.commit()

    repository = ScheduleRepository(database_path)
    repository.initialize()

    assert repository.get_profile(123) == {
        "user_id": 123,
        "role": "student",
        "target": "ПД-12",
        "notifications": False,
    }
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert connection.execute(
            "SELECT COUNT(*) FROM chat_profiles"
        ).fetchone()[0] == 0


def test_import_is_idempotent_and_queryable(
    tmp_path: Path, schedule_workbook: Path
) -> None:
    repository = ScheduleRepository(tmp_path / "schedule.db")
    parsed = ExcelScheduleParser().parse(schedule_workbook)

    first = repository.import_schedule(parsed, sha256="first")
    second = repository.import_schedule(parsed, sha256="first")

    assert first.skipped is False
    assert first.changes
    assert second.skipped is True
    lessons = repository.lessons_for(
        role="student", target="ПД-12", lesson_date=date(2026, 9, 22)
    )
    assert lessons[0].subject == "Теория гос. и права"
    assert repository.available_dates(from_date=date(2026, 9, 22)) == [
        date(2026, 9, 22),
        date(2026, 9, 23),
    ]


def test_profile_round_trip(tmp_path: Path) -> None:
    repository = ScheduleRepository(tmp_path / "schedule.db")
    repository.save_profile(123, role="student", target="ПД-12")

    assert repository.get_profile(123) == {
        "user_id": 123,
        "role": "student",
        "target": "ПД-12",
        "notifications": True,
    }
    repository.set_notifications(123, False)
    assert repository.get_profile(123)["notifications"] is False


def test_chat_profile_crud_and_subscribers(tmp_path: Path) -> None:
    repository = ScheduleRepository(tmp_path / "schedule.db")
    repository.save_chat_profile(
        -100123,
        chat_type="supergroup",
        chat_title="ИС-22",
        target="ИС-22",
        configured_by=456,
    )

    profile = repository.get_chat_profile(-100123)
    assert profile is not None
    assert profile["chat_type"] == "supergroup"
    assert profile["chat_title"] == "ИС-22"
    assert profile["target"] == "ИС-22"
    assert profile["notifications"] is True
    assert profile["pin_enabled"] is False
    assert profile["last_pinned_message_id"] is None
    assert profile["configured_by"] == 456
    assert profile["active"] is True
    assert profile["created_at"]
    assert profile["updated_at"]

    repository.set_chat_pin(-100123, True)
    repository.set_chat_last_message(-100123, 789)
    assert repository.chat_subscribers()[0]["last_pinned_message_id"] == 789
    assert repository.get_chat_profile(-100123)["pin_enabled"] is True

    repository.save_chat_profile(
        -100123,
        chat_type="supergroup",
        chat_title="Новый заголовок",
        target="ПД-12",
        configured_by=999,
    )
    reconfigured = repository.get_chat_profile(-100123)
    assert reconfigured["target"] == "ПД-12"
    assert reconfigured["notifications"] is True
    assert reconfigured["pin_enabled"] is True
    assert reconfigured["last_pinned_message_id"] == 789

    repository.set_chat_notifications(-100123, False)
    assert repository.chat_subscribers() == []
    repository.set_chat_notifications(-100123, True)
    repository.deactivate_chat(-100123)
    assert repository.get_chat_profile(-100123)["active"] is False
    assert repository.chat_subscribers() == []


def test_usage_stats_and_pruning(tmp_path: Path) -> None:
    repository = ScheduleRepository(tmp_path / "schedule.db")
    repository.save_profile(123, role="student", target="ПД-12")
    repository.save_chat_profile(
        -100123,
        chat_type="group",
        chat_title=None,
        target="ИС-22",
        configured_by=123,
    )
    now = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)
    old = now - timedelta(days=100)
    repository.record_usage_event(
        "old-actor",
        chat_type="private",
        event_type="schedule_view",
        properties={"scope": "today", "result": "found"},
        timestamp=old,
    )
    repository.record_usage_event(
        "actor-a",
        chat_type="private",
        event_type="schedule_view",
        properties={"scope": "today", "result": "found"},
        timestamp=now,
    )
    repository.record_usage_event(
        "actor-a",
        chat_type="private",
        event_type="schedule_view",
        properties={"scope": "tomorrow", "result": "empty"},
        timestamp=now + timedelta(seconds=1),
    )
    repository.record_usage_event(
        "actor-b",
        chat_type="group",
        event_type="settings_opened",
        timestamp=now + timedelta(seconds=2),
    )

    assert repository.usage_stats(now - timedelta(minutes=1)) == {
        "active_actors": 2,
        "events_by_type": {"schedule_view": 2, "settings_opened": 1},
        "schedule_scopes": {"today": 1, "tomorrow": 1},
        "schedule_results": {"found": 1, "empty": 1},
        "private_profiles": 1,
        "active_chats": 1,
        "sent_notifications": 0,
        "pending_notifications": 0,
        "failed_notifications": 0,
    }
    assert repository.prune_usage_events(now - timedelta(days=90)) == 1
    assert repository.prune_usage_events(now - timedelta(days=90)) == 0


def test_delete_user_data_removes_personal_references(tmp_path: Path) -> None:
    repository = ScheduleRepository(tmp_path / "schedule.db")
    repository.save_profile(123, role="student", target="ПД-12")
    repository.save_chat_profile(
        -100123,
        chat_type="group",
        chat_title="ПД-12",
        target="ПД-12",
        configured_by=123,
    )
    repository.record_usage_event(
        "actor-key",
        chat_type="private",
        event_type="bot_started",
    )

    assert repository.delete_user_data(123, "actor-key") is True
    assert repository.get_profile(123) is None
    assert repository.get_chat_profile(-100123)["configured_by"] == 0
    assert repository.usage_stats(datetime.now(timezone.utc) - timedelta(days=1))[
        "active_actors"
    ] == 0


def test_teacher_query_is_unicode_case_insensitive(
    tmp_path: Path, schedule_workbook: Path
) -> None:
    repository = ScheduleRepository(tmp_path / "schedule.db")
    parsed = ExcelScheduleParser().parse(schedule_workbook)
    repository.import_schedule(parsed, sha256="teacher-search")

    lessons = repository.lessons_for(
        role="teacher",
        target="яцишина н.в.",
        lesson_date=date(2026, 9, 22),
    )

    assert lessons
    assert all("Яцишина Н.В." in (lesson.teacher or "") for lesson in lessons)
