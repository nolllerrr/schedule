from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Literal

from schedule_bot.domain import (
    ImportResult,
    Lesson,
    ParsedSchedule,
    ScheduleChange,
)


class ScheduleRepository:
    SCHEMA_VERSION = 1

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            current_version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
            if current_version > self.SCHEMA_VERSION:
                raise RuntimeError(
                    "Database schema is newer than this application version"
                )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS imports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_url TEXT,
                    source_filename TEXT NOT NULL,
                    sha256 TEXT NOT NULL UNIQUE,
                    imported_at TEXT NOT NULL,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    warnings_json TEXT NOT NULL DEFAULT '[]'
                );

                CREATE TABLE IF NOT EXISTS schedule_dates (
                    import_id INTEGER NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
                    lesson_date TEXT NOT NULL,
                    PRIMARY KEY (import_id, lesson_date)
                );

                CREATE TABLE IF NOT EXISTS lessons (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    import_id INTEGER NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
                    is_current INTEGER NOT NULL DEFAULT 1,
                    lesson_date TEXT NOT NULL,
                    group_name TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    lesson_label TEXT NOT NULL,
                    lesson_number INTEGER,
                    start_time TEXT,
                    end_time TEXT,
                    subject TEXT NOT NULL,
                    teacher TEXT,
                    room TEXT,
                    subgroup TEXT,
                    raw_json TEXT NOT NULL,
                    UNIQUE (import_id, lesson_date, group_name, position)
                );

                CREATE INDEX IF NOT EXISTS idx_lessons_current_date_group
                ON lessons(is_current, lesson_date, group_name, position);

                CREATE INDEX IF NOT EXISTS idx_lessons_current_teacher
                ON lessons(is_current, lesson_date, teacher);

                CREATE TABLE IF NOT EXISTS user_profiles (
                    user_id INTEGER PRIMARY KEY,
                    role TEXT NOT NULL CHECK(role IN ('student', 'teacher')),
                    target TEXT NOT NULL,
                    notifications INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );
                """
            )
            connection.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
            connection.commit()

    def import_schedule(
        self,
        parsed: ParsedSchedule,
        *,
        sha256: str,
        source_url: str | None = None,
    ) -> ImportResult:
        self.initialize()
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT id FROM imports WHERE sha256 = ?", (sha256,)
            ).fetchone()
            if existing:
                return ImportResult(
                    import_id=int(existing["id"]),
                    skipped=True,
                    changes=(),
                    parsed=parsed,
                )

            old_lessons = self._current_for_dates(connection, parsed.dates)
            old_by_key = {self._lesson_key(item): item for item in old_lessons}
            new_by_key = {self._lesson_key(item): item for item in parsed.lessons}
            changes = self._compare(old_by_key, new_by_key)

            now = datetime.now(timezone.utc).isoformat()
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    INSERT INTO imports(
                        source_url, source_filename, sha256, imported_at,
                        start_date, end_date, warnings_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source_url,
                        parsed.source_filename,
                        sha256,
                        now,
                        parsed.start_date.isoformat(),
                        parsed.end_date.isoformat(),
                        json.dumps(parsed.warnings, ensure_ascii=False),
                    ),
                )
                import_id = int(cursor.lastrowid)

                date_values = tuple(item.isoformat() for item in parsed.dates)
                connection.executemany(
                    "INSERT INTO schedule_dates(import_id, lesson_date) VALUES (?, ?)",
                    ((import_id, value) for value in date_values),
                )
                if date_values:
                    placeholders = ",".join("?" for _ in date_values)
                    connection.execute(
                        f"UPDATE lessons SET is_current = 0 "
                        f"WHERE is_current = 1 AND lesson_date IN ({placeholders})",
                        date_values,
                    )

                connection.executemany(
                    """
                    INSERT INTO lessons(
                        import_id, is_current, lesson_date, group_name, position,
                        lesson_label, lesson_number, start_time, end_time, subject,
                        teacher, room, subgroup, raw_json
                    ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        (
                            import_id,
                            lesson.lesson_date.isoformat(),
                            lesson.group_name,
                            lesson.position,
                            lesson.lesson_label,
                            lesson.lesson_number,
                            lesson.start_time,
                            lesson.end_time,
                            lesson.subject,
                            lesson.teacher,
                            lesson.room,
                            lesson.subgroup,
                            json.dumps(lesson.raw_data, ensure_ascii=False),
                        )
                        for lesson in parsed.lessons
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        return ImportResult(
            import_id=import_id,
            skipped=False,
            changes=changes,
            parsed=parsed,
        )

    def lessons_for(
        self,
        *,
        role: Literal["student", "teacher"],
        target: str,
        lesson_date: date,
    ) -> list[Lesson]:
        self.initialize()
        with self._connect() as connection:
            if role == "student":
                rows = connection.execute(
                    """
                    SELECT * FROM lessons
                    WHERE is_current = 1 AND lesson_date = ? AND group_name = ?
                    ORDER BY position
                    """,
                    (lesson_date.isoformat(), target),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM lessons
                    WHERE is_current = 1 AND lesson_date = ?
                      AND teacher IS NOT NULL
                    ORDER BY position, group_name
                    """,
                    (lesson_date.isoformat(),),
                ).fetchall()
        lessons = [self._row_to_lesson(row) for row in rows]
        if role == "teacher":
            needle = target.casefold()
            lessons = [
                lesson
                for lesson in lessons
                if lesson.teacher and needle in lesson.teacher.casefold()
            ]
        return lessons

    def list_groups(self) -> list[str]:
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT group_name FROM lessons
                WHERE is_current = 1 ORDER BY group_name
                """
            ).fetchall()
        return [str(row["group_name"]) for row in rows]

    def available_dates(self, *, from_date: date, limit: int = 14) -> list[date]:
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT lesson_date FROM lessons
                WHERE is_current = 1 AND lesson_date >= ?
                ORDER BY lesson_date
                LIMIT ?
                """,
                (from_date.isoformat(), limit),
            ).fetchall()
            if not rows:
                rows = connection.execute(
                    """
                    SELECT DISTINCT lesson_date FROM lessons
                    WHERE is_current = 1
                    ORDER BY lesson_date DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                rows = list(reversed(rows))
        return [date.fromisoformat(str(row["lesson_date"])) for row in rows]

    def search_teachers(self, query: str, *, limit: int = 20) -> list[str]:
        self.initialize()
        needle = query.strip().casefold()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT teacher FROM lessons
                WHERE is_current = 1 AND teacher IS NOT NULL
                """
            ).fetchall()
        names: set[str] = set()
        for row in rows:
            for name in re.split(r"\s*/\s*|\s*,\s*", str(row["teacher"])):
                normalized = " ".join(name.split())
                if normalized and needle in normalized.casefold():
                    names.add(normalized)
        return sorted(names)[:limit]

    def save_profile(
        self,
        user_id: int,
        *,
        role: Literal["student", "teacher"],
        target: str,
        notifications: bool = True,
    ) -> None:
        self.initialize()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO user_profiles(user_id, role, target, notifications, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    role = excluded.role,
                    target = excluded.target,
                    notifications = excluded.notifications,
                    updated_at = excluded.updated_at
                """,
                (
                    user_id,
                    role,
                    target,
                    int(notifications),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            connection.commit()

    def get_profile(self, user_id: int) -> dict[str, object] | None:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM user_profiles WHERE user_id = ?", (user_id,)
            ).fetchone()
        if row is None:
            return None
        return {
            "user_id": int(row["user_id"]),
            "role": str(row["role"]),
            "target": str(row["target"]),
            "notifications": bool(row["notifications"]),
        }

    def set_notifications(self, user_id: int, enabled: bool) -> None:
        self.initialize()
        with self._connect() as connection:
            connection.execute(
                "UPDATE user_profiles SET notifications = ?, updated_at = ? WHERE user_id = ?",
                (
                    int(enabled),
                    datetime.now(timezone.utc).isoformat(),
                    user_id,
                ),
            )
            connection.commit()

    def subscribers(self) -> list[dict[str, object]]:
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM user_profiles WHERE notifications = 1"
            ).fetchall()
        return [
            {
                "user_id": int(row["user_id"]),
                "role": str(row["role"]),
                "target": str(row["target"]),
            }
            for row in rows
        ]

    @staticmethod
    def _lesson_key(lesson: Lesson) -> tuple[date, str, int]:
        return lesson.lesson_date, lesson.group_name, lesson.position

    @staticmethod
    def _compare(
        before: dict[tuple[date, str, int], Lesson],
        after: dict[tuple[date, str, int], Lesson],
    ) -> tuple[ScheduleChange, ...]:
        changes: list[ScheduleChange] = []
        for key in sorted(set(before) | set(after)):
            old = before.get(key)
            new = after.get(key)
            if old is None:
                changes.append(ScheduleChange("added", None, new))
            elif new is None:
                changes.append(ScheduleChange("removed", old, None))
            elif old.comparable_data() != new.comparable_data():
                changes.append(ScheduleChange("modified", old, new))
        return tuple(changes)

    def _current_for_dates(
        self, connection: sqlite3.Connection, dates: Iterable[date]
    ) -> list[Lesson]:
        values = tuple(item.isoformat() for item in dates)
        if not values:
            return []
        placeholders = ",".join("?" for _ in values)
        rows = connection.execute(
            f"SELECT * FROM lessons WHERE is_current = 1 "
            f"AND lesson_date IN ({placeholders})",
            values,
        ).fetchall()
        return [self._row_to_lesson(row) for row in rows]

    @staticmethod
    def _row_to_lesson(row: sqlite3.Row) -> Lesson:
        return Lesson(
            lesson_date=date.fromisoformat(str(row["lesson_date"])),
            group_name=str(row["group_name"]),
            position=int(row["position"]),
            lesson_label=str(row["lesson_label"]),
            lesson_number=(
                int(row["lesson_number"])
                if row["lesson_number"] is not None
                else None
            ),
            start_time=row["start_time"],
            end_time=row["end_time"],
            subject=str(row["subject"]),
            teacher=row["teacher"],
            room=row["room"],
            subgroup=row["subgroup"],
            raw_data=json.loads(str(row["raw_json"])),
        )
