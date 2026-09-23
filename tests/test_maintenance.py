from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from schedule_bot.maintenance import backup_before_migration, create_database_backup
from schedule_bot.parser import ExcelScheduleParser
from schedule_bot.repository import ScheduleRepository


def test_backup_preserves_database_contents(
    tmp_path: Path,
    schedule_workbook: Path,
) -> None:
    repository = ScheduleRepository(tmp_path / "schedule.db")
    parsed = ExcelScheduleParser().parse(schedule_workbook)
    repository.import_schedule(parsed, sha256="backup-test")
    repository.save_profile(123, role="student", target="ПД-12")

    backup_path = create_database_backup(
        repository,
        tmp_path / "backups",
        now=datetime(2026, 9, 23, 3, 30),
    )

    assert backup_path.exists()
    with sqlite3.connect(backup_path) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 1
        assert connection.execute(
            "SELECT target FROM user_profiles WHERE user_id = 123"
        ).fetchone()[0] == "ПД-12"


def test_backup_retention_keeps_newest_files(tmp_path: Path) -> None:
    repository = ScheduleRepository(tmp_path / "schedule.db")
    start = datetime(2026, 9, 20, 3, 30)
    created = [
        create_database_backup(
            repository,
            tmp_path / "backups",
            retention_count=2,
            now=start + timedelta(days=offset),
        )
        for offset in range(3)
    ]

    remaining = sorted((tmp_path / "backups").glob("schedule-*.db"))
    assert remaining == created[1:]
    assert not created[0].exists()


def test_pre_migration_backup_preserves_old_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "schedule.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE user_profiles (
                user_id INTEGER PRIMARY KEY,
                role TEXT NOT NULL,
                target TEXT NOT NULL,
                notifications INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO user_profiles
                (user_id, role, target, notifications, updated_at)
            VALUES (123, 'student', 'ИС-22', 1, '2026-09-23T10:00:00+00:00')
            """
        )
        connection.execute("PRAGMA user_version = 1")
        connection.commit()

    repository = ScheduleRepository(database_path)
    backup_path = backup_before_migration(
        repository,
        tmp_path / "backups",
        now=datetime(2026, 9, 23, 3, 0),
    )
    assert backup_path is not None
    repository.initialize()

    with sqlite3.connect(backup_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute(
            "SELECT target FROM user_profiles WHERE user_id = 123"
        ).fetchone()[0] == "ИС-22"
    assert repository.current_schema_version() == repository.SCHEMA_VERSION
    assert backup_before_migration(repository, tmp_path / "backups") is None
