from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from schedule_bot.domain import Lesson, LessonSegment, ParsedSchedule
from schedule_bot.freshness import freshness_notice, with_freshness
from schedule_bot.matching import compare_lessons
from schedule_bot.repository import ScheduleRepository
from schedule_bot.runtime_health import read_runtime_health, run_runtime_health

DAY = date(2026, 10, 14)
NOW = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)


def lesson(subject, position, **values):
    return Lesson(DAY, "A-1", position, f"{position} пара", position, f"{8+position:02d}:00", f"{9+position:02d}:00", subject, **values)


@pytest.mark.parametrize("old,new,expected", [
    (["Math", "Physics", "English"], ["History", "Math", "Physics", "English"], {"added": 1, "moved": 3}),
    (["Math", "Physics", "English"], ["Math", "English"], {"removed": 1, "moved": 1}),
    (["Math", "Physics", "English"], ["Physics", "Math", "English"], {"moved": 2}),
    (["Math", "Math", "English"], ["Math", "English", "Math"], {"moved": 2}),
])
def test_meaningful_slot_changes(old, new, expected):
    changes = compare_lessons([lesson(item, n+1) for n, item in enumerate(old)], [lesson(item, n+1) for n, item in enumerate(new)])
    assert {kind: sum(item.kind == kind for item in changes) for kind in {item.kind for item in changes}} == expected
    assert all(item.before is None or item.after is None or item.before.subject == item.after.subject for item in changes)


def test_geometry_alone_and_teacher_replacement():
    original = lesson("Math", 1, teacher="Teacher A")
    assert not compare_lessons([original], [replace(original, position=2)])
    changes = compare_lessons([original], [replace(original, teacher="Teacher B")])
    assert len(changes) == 1 and changes[0].kind == "modified"
    assert len(compare_lessons([original], [replace(original, end_time="12:00")])) == 1


def test_freshness_distinguishes_fetch_from_acceptance_and_archive_age():
    state = {"last_fetch_at": NOW.isoformat(), "last_accepted_at": None, "last_error_code": None}
    assert "не подтверждена" in freshness_notice(state, now=NOW)
    state["last_accepted_at"] = NOW.isoformat()
    assert "устаревшим" not in freshness_notice(state, now=NOW)
    state["last_error_code"] = "ImportQuarantinedError"
    assert "устаревшим" in freshness_notice(state, now=NOW)
    state["last_error_code"] = None
    assert "устаревшим" in freshness_notice(state, now=NOW+timedelta(hours=3))
    assert freshness_notice(state, now=NOW, show_success=False) == ""


def test_segment_storage_and_old_hmac_deletion(tmp_path):
    repo = ScheduleRepository(tmp_path / "segments.db")
    parts = (LessonSegment("Math", "Teacher A", "101", "1"), LessonSegment("Physics", "Teacher B", "202", "2"))
    slot = lesson("legacy", 1).with_segments(parts)
    repo.import_schedule(ParsedSchedule("14.10.2026.xlsx", DAY, DAY, (DAY,), (slot,)), sha256="segments")
    projected = repo.lessons_for(role="teacher", target="Teacher A", lesson_date=DAY)
    assert projected[0].segments == parts[:1]
    assert "Physics" not in projected[0].subject and "202" not in projected[0].room
    for key in ("old-hmac", "new-hmac", "other"):
        repo.record_usage_event(key, chat_type="private", event_type="test")
    assert repo.delete_user_data(101, ("old-hmac", "new-hmac"))
    with closing(sqlite3.connect(repo.database_path)) as conn, conn:
        assert conn.execute("SELECT actor_key FROM usage_events").fetchall() == [("other",)]


@pytest.mark.asyncio
async def test_unknown_freshness_is_visible_in_menu(tmp_path):
    repo = ScheduleRepository(tmp_path / "fresh.db")
    text = await with_freshness("Menu", repo)
    assert "Menu" in text and "не подтверждена" in text


@pytest.mark.asyncio
async def test_health_heartbeat_expires_and_is_removed_on_shutdown(tmp_path):
    path = tmp_path / "health.json"
    worker = asyncio.create_task(asyncio.sleep(30))
    monitor = asyncio.create_task(run_runtime_health(path, 9, worker, SimpleNamespace(running=True), lambda: True, interval=0.01))
    try:
        for _ in range(100):
            if path.exists():
                break
            await asyncio.sleep(0.01)
        assert read_runtime_health(path)["ready"] is True
    finally:
        monitor.cancel()
        with pytest.raises(asyncio.CancelledError):
            await monitor
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
    assert not path.exists()
    path.write_text(json.dumps({"ready": True, "checked_at": "2020-01-01T00:00:00+00:00"}), encoding="utf-8")
    assert read_runtime_health(path)["ready"] is False


def test_v9_failure_rolls_back_all_new_columns(tmp_path, monkeypatch):
    repo = ScheduleRepository(tmp_path / "migration.db")
    repo.SCHEMA_VERSION = 8
    repo.initialize()
    newest = ScheduleRepository(repo.database_path)
    original = newest._migrate_to_v9
    def fail(connection):
        original(connection)
        raise sqlite3.OperationalError("Injected migration failure")
    monkeypatch.setattr(newest, "_migrate_to_v9", fail)
    with pytest.raises(sqlite3.OperationalError):
        newest.initialize()
    assert newest.current_schema_version() == 8
    with closing(sqlite3.connect(repo.database_path)) as conn, conn:
        assert "segments_json" not in {item[1] for item in conn.execute("PRAGMA table_info(lessons)")}


def test_critical_repeat_coalesces_per_recipient_and_survives_restart(tmp_path):
    path = tmp_path / "alerts.db"
    repo = ScheduleRepository(path)
    assert repo.enqueue_admin_error("HTTP 403", (101,), now=NOW, dedupe_seconds=21600) == 1
    repo = ScheduleRepository(path)
    assert repo.enqueue_admin_error("HTTP 403", (101, 102), now=NOW+timedelta(hours=1), dedupe_seconds=21600) == 1
    assert repo.enqueue_admin_error("Invalid workbook", (101,), now=NOW+timedelta(hours=1), dedupe_seconds=21600) == 1
    assert repo.enqueue_admin_error("HTTP 403", (101,), now=NOW+timedelta(days=1), dedupe_seconds=21600) == 0
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("UPDATE admin_notification_outbox SET status='sent'")
    assert repo.enqueue_admin_error("HTTP 403", (101,), now=NOW+timedelta(hours=6), dedupe_seconds=21600) == 1
