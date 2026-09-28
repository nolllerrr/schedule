from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Literal, Mapping

from schedule_bot.domain import (
    ImportResult,
    Lesson,
    ParsedSchedule,
    ScheduleChange,
)


class ScheduleRepository:
    SCHEMA_VERSION = 4
    BUSY_TIMEOUT_MS = 5_000

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self._initialized = False

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.database_path,
            timeout=self.BUSY_TIMEOUT_MS / 1_000,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {self.BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        if self._initialized:
            return
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            current_version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
            if current_version > self.SCHEMA_VERSION:
                raise RuntimeError(
                    "Database schema is newer than this application version"
                )
            # WAL is persistent for a database; avoid requesting a mode change
            # once an earlier initialization has already enabled it.
            journal_mode = str(
                connection.execute("PRAGMA journal_mode").fetchone()[0]
            ).casefold()
            if journal_mode != "wal":
                connection.execute("PRAGMA journal_mode = WAL")
            while current_version < self.SCHEMA_VERSION:
                target_version = current_version + 1
                connection.execute("BEGIN IMMEDIATE")
                try:
                    # Another process may have completed the migration while this
                    # connection was waiting for the write lock.
                    locked_version = int(
                        connection.execute("PRAGMA user_version").fetchone()[0]
                    )
                    if locked_version != current_version:
                        connection.rollback()
                        current_version = locked_version
                        if current_version > self.SCHEMA_VERSION:
                            raise RuntimeError(
                                "Database schema is newer than this application version"
                            )
                        continue

                    if target_version == 1:
                        self._migrate_to_v1(connection)
                    elif target_version == 2:
                        self._migrate_to_v2(connection)
                    elif target_version == 3:
                        self._migrate_to_v3(connection)
                    elif target_version == 4:
                        self._migrate_to_v4(connection)
                    else:  # pragma: no cover - guards future migration mistakes
                        raise RuntimeError(
                            f"Missing database migration to version {target_version}"
                        )

                    connection.execute(f"PRAGMA user_version = {target_version}")
                    connection.commit()
                    current_version = target_version
                except Exception:
                    connection.rollback()
                    raise
        self._initialized = True

    def current_schema_version(self) -> int:
        if not self.database_path.exists():
            return 0
        with self._connect() as connection:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])

    def backup_to(
        self,
        destination: str | Path,
        *,
        initialize: bool = True,
    ) -> Path:
        """Create a consistent SQLite backup without stopping the bot."""
        if initialize:
            self.initialize()
        elif not self.database_path.exists():
            raise FileNotFoundError(self.database_path)
        destination_path = Path(destination)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        if self.database_path.resolve() == destination_path.resolve():
            raise ValueError("Backup destination must differ from the database path")

        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=f".{destination_path.name}.",
                suffix=".tmp",
                dir=destination_path.parent,
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)

            with self._connect() as source, closing(
                sqlite3.connect(temporary_path)
            ) as backup:
                source.backup(backup)
                check = backup.execute("PRAGMA quick_check").fetchone()
                if check is None or str(check[0]).casefold() != "ok":
                    raise RuntimeError("SQLite backup integrity check failed")

            os.replace(temporary_path, destination_path)
            temporary_path = None
            return destination_path
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _migrate_to_v1(connection: sqlite3.Connection) -> None:
        statements = (
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
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS schedule_dates (
                import_id INTEGER NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
                lesson_date TEXT NOT NULL,
                PRIMARY KEY (import_id, lesson_date)
            )
            """,
            """
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
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_lessons_current_date_group
            ON lessons(is_current, lesson_date, group_name, position)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_lessons_current_teacher
            ON lessons(is_current, lesson_date, teacher)
            """,
            """
            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id INTEGER PRIMARY KEY,
                role TEXT NOT NULL CHECK(role IN ('student', 'teacher')),
                target TEXT NOT NULL,
                notifications INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            )
            """,
        )
        for statement in statements:
            connection.execute(statement)

    @staticmethod
    def _migrate_to_v2(connection: sqlite3.Connection) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS chat_profiles (
                chat_id INTEGER PRIMARY KEY,
                chat_type TEXT NOT NULL CHECK(chat_type IN ('group', 'supergroup')),
                chat_title TEXT,
                target TEXT NOT NULL,
                notifications INTEGER NOT NULL DEFAULT 1,
                pin_enabled INTEGER NOT NULL DEFAULT 0,
                last_pinned_message_id INTEGER,
                configured_by INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_chat_profiles_subscribers
            ON chat_profiles(active, notifications)
            """,
            """
            CREATE TABLE IF NOT EXISTS usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                actor_key TEXT NOT NULL,
                chat_type TEXT NOT NULL
                    CHECK(chat_type IN ('private', 'group', 'supergroup')),
                event_type TEXT NOT NULL,
                properties_json TEXT NOT NULL DEFAULT '{}'
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_usage_events_timestamp
            ON usage_events(timestamp)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_usage_events_type_timestamp
            ON usage_events(event_type, timestamp)
            """,
        )
        for statement in statements:
            connection.execute(statement)

    @staticmethod
    def _migrate_to_v3(connection: sqlite3.Connection) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS notification_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                import_id INTEGER NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
                destination_id INTEGER NOT NULL,
                destination_kind TEXT NOT NULL
                    CHECK(destination_kind IN ('user', 'chat')),
                chat_type TEXT NOT NULL
                    CHECK(chat_type IN ('private', 'group', 'supergroup')),
                chunk_index INTEGER NOT NULL,
                text TEXT NOT NULL,
                pin_requested INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'sent', 'failed')),
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                delivered_at TEXT,
                last_error TEXT,
                UNIQUE(import_id, destination_id, destination_kind, chunk_index)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_notification_outbox_pending
            ON notification_outbox(status, attempts, id)
            """,
        )
        for statement in statements:
            connection.execute(statement)

    @staticmethod
    def _migrate_to_v4(connection: sqlite3.Connection) -> None:
        connection.execute(
            "ALTER TABLE chat_profiles ADD COLUMN last_pinned_schedule_date TEXT"
        )
        connection.execute(
            "ALTER TABLE chat_profiles ADD COLUMN last_pinned_schedule_hash TEXT"
        )

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

            now = datetime.now(timezone.utc).isoformat()
            try:
                connection.execute("BEGIN IMMEDIATE")
                published_dates = {
                    date.fromisoformat(str(row["lesson_date"]))
                    for row in connection.execute(
                        "SELECT DISTINCT lesson_date FROM schedule_dates"
                    )
                }
                new_dates = tuple(sorted(set(parsed.dates) - published_dates))
                old_lessons = self._current_for_dates(connection, parsed.dates)
                old_by_key = {self._lesson_key(item): item for item in old_lessons}
                new_by_key = {self._lesson_key(item): item for item in parsed.lessons}
                changes = self._compare(old_by_key, new_by_key)
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
            new_dates=new_dates,
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

    def published_dates(self) -> list[date]:
        """All dates covered by imported workbooks, including past dates."""
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT lesson_date FROM schedule_dates
                ORDER BY lesson_date
                """
            ).fetchall()
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

    def delete_user_data(self, user_id: int, actor_key: str) -> bool:
        self.initialize()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            profile_cursor = connection.execute(
                "DELETE FROM user_profiles WHERE user_id = ?", (user_id,)
            )
            chat_cursor = connection.execute(
                """
                UPDATE chat_profiles
                SET configured_by = 0, updated_at = ?
                WHERE configured_by = ?
                """,
                (datetime.now(timezone.utc).isoformat(), user_id),
            )
            events_cursor = connection.execute(
                "DELETE FROM usage_events WHERE actor_key = ?", (actor_key,)
            )
            connection.commit()
            return any(
                cursor.rowcount > 0
                for cursor in (profile_cursor, chat_cursor, events_cursor)
            )

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

    def save_chat_profile(
        self,
        chat_id: int,
        *,
        chat_type: Literal["group", "supergroup"],
        chat_title: str | None,
        target: str,
        configured_by: int,
        notifications: bool = True,
        pin_enabled: bool = False,
    ) -> None:
        self.initialize()
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO chat_profiles(
                    chat_id, chat_type, chat_title, target, notifications,
                    pin_enabled, configured_by, active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    chat_type = excluded.chat_type,
                    chat_title = excluded.chat_title,
                    target = excluded.target,
                    configured_by = excluded.configured_by,
                    active = 1,
                    updated_at = excluded.updated_at
                """,
                (
                    chat_id,
                    chat_type,
                    chat_title,
                    target,
                    int(notifications),
                    int(pin_enabled),
                    configured_by,
                    now,
                    now,
                ),
            )
            connection.commit()

    def get_chat_profile(self, chat_id: int) -> dict[str, object] | None:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM chat_profiles WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        return self._row_to_chat_profile(row) if row is not None else None

    def set_chat_notifications(self, chat_id: int, enabled: bool) -> None:
        self._update_chat_profile(chat_id, notifications=int(enabled))

    def set_chat_pin(self, chat_id: int, enabled: bool) -> None:
        self._update_chat_profile(chat_id, pin_enabled=int(enabled))

    def set_chat_last_message(self, chat_id: int, message_id: int | None) -> None:
        self._update_chat_profile(chat_id, last_pinned_message_id=message_id)

    def set_chat_last_pin(
        self,
        chat_id: int,
        *,
        message_id: int | None,
        schedule_date: date | None,
        schedule_hash: str | None,
    ) -> None:
        self._update_chat_profile(
            chat_id,
            last_pinned_message_id=message_id,
            last_pinned_schedule_date=(
                schedule_date.isoformat() if schedule_date is not None else None
            ),
            last_pinned_schedule_hash=schedule_hash,
        )

    def deactivate_chat(self, chat_id: int) -> None:
        self._update_chat_profile(chat_id, active=0)

    def chat_subscribers(self) -> list[dict[str, object]]:
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM chat_profiles
                WHERE active = 1 AND notifications = 1
                ORDER BY chat_id
                """
            ).fetchall()
        return [self._row_to_chat_profile(row) for row in rows]

    def pinned_chats(self) -> list[dict[str, object]]:
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM chat_profiles
                WHERE active = 1 AND pin_enabled = 1
                ORDER BY chat_id
                """
            ).fetchall()
        return [self._row_to_chat_profile(row) for row in rows]

    def enqueue_notifications(
        self,
        deliveries: Iterable[Mapping[str, object]],
    ) -> None:
        self.initialize()
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            (
                int(item["import_id"]),
                int(item["destination_id"]),
                str(item["destination_kind"]),
                str(item["chat_type"]),
                int(item["chunk_index"]),
                str(item["text"]),
                int(bool(item.get("pin_requested", False))),
                now,
            )
            for item in deliveries
        ]
        if not rows:
            return
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT OR IGNORE INTO notification_outbox(
                    import_id, destination_id, destination_kind, chat_type,
                    chunk_index, text, pin_requested, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            connection.commit()

    def pending_notifications(
        self,
        *,
        limit: int = 100,
        max_attempts: int = 5,
    ) -> list[dict[str, object]]:
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT current.* FROM notification_outbox AS current
                WHERE current.status = 'pending' AND current.attempts < ?
                  AND NOT EXISTS (
                      SELECT 1 FROM notification_outbox AS earlier
                      WHERE earlier.import_id = current.import_id
                        AND earlier.destination_id = current.destination_id
                        AND earlier.destination_kind = current.destination_kind
                        AND earlier.chunk_index < current.chunk_index
                        AND earlier.status = 'pending'
                  )
                ORDER BY current.import_id, current.chunk_index, current.id
                LIMIT ?
                """,
                (max_attempts, limit),
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "import_id": int(row["import_id"]),
                "destination_id": int(row["destination_id"]),
                "destination_kind": str(row["destination_kind"]),
                "chat_type": str(row["chat_type"]),
                "chunk_index": int(row["chunk_index"]),
                "text": str(row["text"]),
                "pin_requested": bool(row["pin_requested"]),
                "attempts": int(row["attempts"]),
            }
            for row in rows
        ]

    def mark_notification_sent(self, delivery_id: int) -> None:
        self.initialize()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE notification_outbox
                SET status = 'sent', delivered_at = ?, last_error = NULL
                WHERE id = ?
                """,
                (datetime.now(timezone.utc).isoformat(), delivery_id),
            )
            connection.commit()

    def mark_notification_failed(
        self,
        delivery_id: int,
        error: str,
        *,
        max_attempts: int = 5,
        permanent: bool = False,
    ) -> None:
        self.initialize()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE notification_outbox
                SET attempts = attempts + 1,
                    status = CASE
                        WHEN ? OR attempts + 1 >= ? THEN 'failed'
                        ELSE 'pending'
                    END,
                    last_error = ?
                WHERE id = ?
                """,
                (int(permanent), max_attempts, error[:500], delivery_id),
            )
            connection.commit()

    def notification_outbox_counts(self) -> dict[str, int]:
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM notification_outbox
                GROUP BY status
                """
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def record_usage_event(
        self,
        actor_key: str,
        *,
        chat_type: Literal["private", "group", "supergroup"],
        event_type: str,
        properties: Mapping[str, object] | None = None,
        timestamp: datetime | None = None,
    ) -> None:
        self.initialize()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO usage_events(
                    timestamp, actor_key, chat_type, event_type, properties_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    self._utc_timestamp(timestamp),
                    actor_key,
                    chat_type,
                    event_type,
                    json.dumps(dict(properties or {}), ensure_ascii=False),
                ),
            )
            connection.commit()

    def prune_usage_events(self, before: datetime) -> int:
        self.initialize()
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM usage_events WHERE timestamp < ?",
                (self._utc_timestamp(before),),
            )
            connection.commit()
            return cursor.rowcount

    def usage_stats(self, since: datetime) -> dict[str, object]:
        self.initialize()
        with self._connect() as connection:
            event_rows = connection.execute(
                """
                SELECT actor_key, event_type, properties_json
                FROM usage_events
                WHERE timestamp >= ?
                """,
                (self._utc_timestamp(since),),
            ).fetchall()
            private_profiles = int(
                connection.execute("SELECT COUNT(*) FROM user_profiles").fetchone()[0]
            )
            active_chats = int(
                connection.execute(
                    "SELECT COUNT(*) FROM chat_profiles WHERE active = 1"
                ).fetchone()[0]
            )
            outbox_counts = {
                str(row["status"]): int(row["count"])
                for row in connection.execute(
                    """
                    SELECT status, COUNT(*) AS count
                    FROM notification_outbox
                    WHERE created_at >= ?
                    GROUP BY status
                    """,
                    (self._utc_timestamp(since),),
                ).fetchall()
            }

        actors: set[str] = set()
        events_by_type: dict[str, int] = {}
        schedule_scopes: dict[str, int] = {}
        schedule_results: dict[str, int] = {}
        for row in event_rows:
            event_type = str(row["event_type"])
            events_by_type[event_type] = events_by_type.get(event_type, 0) + 1
            try:
                properties = json.loads(str(row["properties_json"]))
            except (json.JSONDecodeError, TypeError):
                properties = {}
            if not isinstance(properties, dict):
                continue
            if properties.get("_actor_kind") in (None, "user"):
                actors.add(str(row["actor_key"]))
            scope = properties.get("scope")
            if isinstance(scope, str) and scope:
                schedule_scopes[scope] = schedule_scopes.get(scope, 0) + 1
            result = properties.get("result")
            if isinstance(result, str) and result:
                schedule_results[result] = schedule_results.get(result, 0) + 1

        return {
            "active_actors": len(actors),
            "events_by_type": events_by_type,
            "schedule_scopes": schedule_scopes,
            "schedule_results": schedule_results,
            "private_profiles": private_profiles,
            "active_chats": active_chats,
            "sent_notifications": outbox_counts.get("sent", 0),
            "pending_notifications": outbox_counts.get("pending", 0),
            "failed_notifications": outbox_counts.get("failed", 0),
        }

    def _update_chat_profile(self, chat_id: int, **values: object) -> None:
        allowed = {
            "notifications",
            "pin_enabled",
            "last_pinned_message_id",
            "last_pinned_schedule_date",
            "last_pinned_schedule_hash",
            "active",
        }
        if not values or not values.keys() <= allowed:
            raise ValueError("Unsupported chat profile update")
        self.initialize()
        values["updated_at"] = datetime.now(timezone.utc).isoformat()
        assignments = ", ".join(f"{column} = ?" for column in values)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE chat_profiles SET {assignments} WHERE chat_id = ?",
                (*values.values(), chat_id),
            )
            connection.commit()

    @staticmethod
    def _row_to_chat_profile(row: sqlite3.Row) -> dict[str, object]:
        return {
            "chat_id": int(row["chat_id"]),
            "chat_type": str(row["chat_type"]),
            "chat_title": (
                str(row["chat_title"]) if row["chat_title"] is not None else None
            ),
            "target": str(row["target"]),
            "notifications": bool(row["notifications"]),
            "pin_enabled": bool(row["pin_enabled"]),
            "last_pinned_message_id": (
                int(row["last_pinned_message_id"])
                if row["last_pinned_message_id"] is not None
                else None
            ),
            "last_pinned_schedule_date": (
                date.fromisoformat(str(row["last_pinned_schedule_date"]))
                if row["last_pinned_schedule_date"] is not None
                else None
            ),
            "last_pinned_schedule_hash": (
                str(row["last_pinned_schedule_hash"])
                if row["last_pinned_schedule_hash"] is not None
                else None
            ),
            "configured_by": int(row["configured_by"]),
            "active": bool(row["active"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    @staticmethod
    def _utc_timestamp(value: datetime | None) -> str:
        if value is None:
            value = datetime.now(timezone.utc)
        elif value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        else:
            value = value.astimezone(timezone.utc)
        return value.isoformat()

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
