from __future__ import annotations

import json
import os
import random
import re
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator, Literal, Mapping

from schedule_bot.domain import (
    ImportResult,
    Lesson,
    LessonSegment,
    ParsedSchedule,
    ScheduleChange,
)
from schedule_bot.change_policy import changes_for_profile, should_notify
from schedule_bot.import_validation import ImportQuarantinedError, validate_import
from schedule_bot.notification_content import notification_chunks


class ScheduleRepository:
    SCHEMA_VERSION = 10
    BUSY_TIMEOUT_MS = 5_000
    NOTIFICATION_TTL = timedelta(days=7)

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
                    elif target_version == 5:
                        self._migrate_to_v5(connection)
                    elif target_version == 6:
                        self._migrate_to_v6(connection)
                    elif target_version == 7:
                        self._migrate_to_v7(connection)
                    elif target_version == 8:
                        self._migrate_to_v8(connection)
                    elif target_version == 9:
                        self._migrate_to_v9(connection)
                    elif target_version == 10:
                        self._migrate_to_v10(connection)
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

    @staticmethod
    def _migrate_to_v5(connection: sqlite3.Connection) -> None:
        # Some legacy v1 databases contain profiles only; complete the v1 tables
        # without replacing any existing data before extending imports.
        ScheduleRepository._migrate_to_v1(connection)
        connection.execute(
            "ALTER TABLE imports ADD COLUMN coverage_json TEXT NOT NULL DEFAULT '{}'"
        )
        connection.execute("ALTER TABLE imports ADD COLUMN approval_reason TEXT")
        connection.execute(
            """
            CREATE TABLE quarantined_imports (
                sha256 TEXT PRIMARY KEY,
                source_filename TEXT NOT NULL,
                source_url TEXT,
                checked_at TEXT NOT NULL,
                reasons_json TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'approved'))
            )
            """
        )

    @staticmethod
    def _migrate_to_v6(connection: sqlite3.Connection) -> None:
        for declaration in (
            "pending_pin_message_id INTEGER",
            "pending_pin_schedule_date TEXT",
            "pending_pin_schedule_hash TEXT",
            "pin_retry_after TEXT",
            "pin_attempts INTEGER NOT NULL DEFAULT 0",
        ):
            connection.execute(f"ALTER TABLE chat_profiles ADD COLUMN {declaration}")

    @staticmethod
    def _migrate_to_v7(connection: sqlite3.Connection) -> None:
        for declaration in (
            "next_attempt_at TEXT NOT NULL DEFAULT ''",
            "expires_at TEXT",
            "failure_kind TEXT",
        ):
            connection.execute(f"ALTER TABLE notification_outbox ADD COLUMN {declaration}")
        for row in connection.execute("SELECT id, created_at FROM notification_outbox").fetchall():
            created = datetime.fromisoformat(str(row["created_at"]))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            expires = created.astimezone(timezone.utc) + ScheduleRepository.NOTIFICATION_TTL
            connection.execute(
                "UPDATE notification_outbox SET expires_at = ?, "
                "failure_kind = CASE WHEN status = 'failed' THEN 'legacy' ELSE NULL END "
                "WHERE id = ?", (expires.isoformat(), row["id"]),
            )
        connection.execute(
            "CREATE INDEX idx_notification_outbox_due "
            "ON notification_outbox(status, next_attempt_at)"
        )
        ScheduleRepository._block_notification_tails(connection)

    @staticmethod
    def _migrate_to_v8(connection: sqlite3.Connection) -> None:
        # Add monitoring tables without touching imports, subscriptions or pins.
        statements = (
            """
            CREATE TABLE schedule_source_health (
                source_key TEXT PRIMARY KEY,
                source_host TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('HEALTHY', 'DEGRADED', 'DOWN')),
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                first_failure_at TEXT,
                last_failure_at TEXT,
                last_success_at TEXT,
                last_error_category TEXT,
                incident_id INTEGER
            )
            """,
            """
            CREATE TABLE schedule_outage_incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT NOT NULL REFERENCES schedule_source_health(source_key),
                started_at TEXT NOT NULL,
                warning_at TEXT,
                recovered_at TEXT
            )
            """,
            """
            CREATE TABLE admin_notification_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                incident_id INTEGER REFERENCES schedule_outage_incidents(id),
                kind TEXT NOT NULL CHECK(kind IN ('warning', 'recovery', 'critical')),
                destination_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'sent', 'failed', 'canceled')),
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                next_attempt_at TEXT NOT NULL,
                delivered_at TEXT,
                last_error TEXT,
                UNIQUE(incident_id, kind, destination_id)
            )
            """,
            "CREATE INDEX idx_admin_notification_outbox_due "
            "ON admin_notification_outbox(status, next_attempt_at, id)",
        )
        for statement in statements:
            connection.execute(statement)

    @staticmethod
    def _migrate_to_v9(connection: sqlite3.Connection) -> None:
        connection.execute("ALTER TABLE lessons ADD COLUMN segments_json TEXT NOT NULL DEFAULT '[]'")
        connection.execute("ALTER TABLE imports ADD COLUMN empty_dates_json TEXT NOT NULL DEFAULT '[]'")
        statements = (
            """CREATE TABLE source_publications (
                publication_key TEXT PRIMARY KEY, source_url TEXT NOT NULL,
                import_id INTEGER NOT NULL REFERENCES imports(id),
                start_date TEXT NOT NULL, end_date TEXT NOT NULL,
                last_seen_at TEXT NOT NULL, present INTEGER NOT NULL DEFAULT 1
            )""",
            """CREATE TABLE active_schedule_dates (
                lesson_date TEXT PRIMARY KEY,
                source_import_id INTEGER NOT NULL REFERENCES imports(id),
                effective_import_id INTEGER NOT NULL REFERENCES imports(id)
            )""",
            """INSERT INTO active_schedule_dates
                SELECT lesson_date, MAX(import_id), MAX(import_id)
                FROM schedule_dates GROUP BY lesson_date""",
            """CREATE TABLE sync_status (
                id INTEGER PRIMARY KEY CHECK(id = 1), last_checked_at TEXT,
                last_fetch_at TEXT, last_accepted_at TEXT, last_error_code TEXT,
                last_import_id INTEGER REFERENCES imports(id)
            )""",
            "INSERT INTO sync_status(id) VALUES (1)",
            """CREATE TABLE delivery_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                notification_id INTEGER REFERENCES notification_outbox(id) ON DELETE CASCADE,
                admin_notification_id INTEGER REFERENCES admin_notification_outbox(id) ON DELETE CASCADE,
                started_at TEXT NOT NULL, completed_at TEXT,
                outcome TEXT NOT NULL DEFAULT 'uncertain'
                    CHECK(outcome IN ('uncertain','accepted','rejected','sent')),
                telegram_message_id INTEGER, error_category TEXT,
                reviewed_at TEXT, review_reason TEXT,
                CHECK((notification_id IS NULL) != (admin_notification_id IS NULL))
            )""",
            "CREATE INDEX idx_delivery_attempts_user ON delivery_attempts(notification_id, id)",
            "CREATE INDEX idx_delivery_attempts_admin ON delivery_attempts(admin_notification_id, id)",
            "CREATE INDEX idx_admin_critical_repeat ON admin_notification_outbox(kind,destination_id,created_at)",
        )
        for statement in statements:
            connection.execute(statement)

    def _migrate_to_v10(self, connection: sqlite3.Connection) -> None:
        # Existing rows remain unknown: never infer their original group from
        # a profile that may have changed since the notification was queued.
        connection.execute("ALTER TABLE notification_outbox ADD COLUMN target_snapshot TEXT")
        connection.execute(
            "CREATE INDEX idx_outbox_chat_import ON notification_outbox"
            "(destination_kind,destination_id,chunk_index,import_id)"
        )

    @staticmethod
    def _source_state(
        connection: sqlite3.Connection, source_key: str
    ) -> dict[str, object] | None:
        row = connection.execute(
            "SELECT * FROM schedule_source_health WHERE source_key = ?", (source_key,)
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["incident_started_at"] = row["first_failure_at"]
        result["warning_sent"] = bool(
            connection.execute(
                "SELECT 1 FROM admin_notification_outbox WHERE incident_id = ? "
                "AND kind = 'warning' AND status = 'sent' LIMIT 1",
                (row["incident_id"],),
            ).fetchone()
        )
        return result

    def source_health(self, source_key: str) -> dict[str, object] | None:
        self.initialize()
        with self._connect() as connection:
            return self._source_state(connection, source_key)

    def has_imported_schedule(self) -> bool:
        self.initialize()
        with self._connect() as connection:
            return (
                connection.execute("SELECT 1 FROM imports LIMIT 1").fetchone()
                is not None
            )

    def reconcile_publications(self, publications, *, approval_reason: str | None = None):
        from schedule_bot.publications import reconcile_publications
        return reconcile_publications(self, publications, approval_reason=approval_reason)

    def record_sync_check(self, *, phase: str, error_code: str | None = None, import_id: int | None = None) -> None:
        self.initialize()
        now = self._utc_timestamp(None)
        with self._connect() as connection:
            if phase == "start":
                connection.execute("UPDATE sync_status SET last_checked_at=? WHERE id=1", (now,))
            elif phase == "fetch":
                connection.execute("UPDATE sync_status SET last_fetch_at=? WHERE id=1", (now,))
            elif phase == "accepted":
                connection.execute("UPDATE sync_status SET last_accepted_at=?, last_error_code=NULL,last_import_id=? WHERE id=1", (now, import_id))
            elif phase == "failed":
                safe_code = error_code if error_code and re.fullmatch(r"[A-Za-z0-9_]{1,80}", error_code) else "update_error"
                connection.execute("UPDATE sync_status SET last_error_code=? WHERE id=1", (safe_code,))
            else:
                raise ValueError("Unknown sync phase")
            connection.commit()

    def schedule_freshness(self) -> dict[str, object]:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM sync_status WHERE id=1").fetchone()
            return dict(row)

    def record_source_failure(
        self,
        source_key: str,
        source_host: str,
        *,
        failure_threshold: int,
        error_category: str,
        admin_ids: tuple[int, ...],
        warning_text: Callable[[dict[str, object]], str],
        now: datetime | None = None,
    ) -> dict[str, object]:
        self.initialize()
        timestamp = self._utc_timestamp(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO schedule_source_health(source_key, source_host, status) "
                "VALUES (?, ?, 'HEALTHY') ON CONFLICT(source_key) DO NOTHING",
                (source_key, source_host),
            )
            previous = self._source_state(connection, source_key)
            assert previous is not None
            failures = int(previous["consecutive_failures"]) + 1
            incident_id = previous["incident_id"]
            first_failure = previous["first_failure_at"]
            if incident_id is None:
                first_failure = timestamp
                incident_id = connection.execute(
                    "INSERT INTO schedule_outage_incidents(source_key, started_at) VALUES (?, ?)",
                    (source_key, timestamp),
                ).lastrowid
            status = (
                "DOWN"
                if previous["status"] == "DOWN" or failures >= failure_threshold
                else "DEGRADED"
            )
            connection.execute(
                "UPDATE schedule_source_health SET status = ?, consecutive_failures = ?, "
                "first_failure_at = ?, last_failure_at = ?, last_error_category = ?, "
                "incident_id = ? WHERE source_key = ?",
                (
                    status,
                    failures,
                    first_failure,
                    timestamp,
                    error_category,
                    incident_id,
                    source_key,
                ),
            )
            state = self._source_state(connection, source_key)
            assert state is not None
            if status == "DOWN" and previous["status"] != "DOWN":
                text = warning_text(state)
                connection.execute(
                    "UPDATE schedule_outage_incidents SET warning_at = ? WHERE id = ?",
                    (timestamp, incident_id),
                )
                connection.executemany(
                    "INSERT INTO admin_notification_outbox"
                    "(incident_id, kind, destination_id, text, created_at, next_attempt_at) "
                    "VALUES (?, 'warning', ?, ?, ?, ?) "
                    "ON CONFLICT(incident_id, kind, destination_id) DO NOTHING",
                    [
                        (incident_id, admin_id, text, timestamp, timestamp)
                        for admin_id in set(admin_ids)
                    ],
                )
            connection.commit()
            return state

    def source_warning_recipients(self, source_key: str) -> tuple[int, ...]:
        self.initialize()
        with self._connect() as connection:
            return tuple(
                int(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT destination_id FROM admin_notification_outbox "
                    "WHERE kind = 'warning' AND incident_id = "
                    "(SELECT incident_id FROM schedule_source_health WHERE source_key = ?) "
                    "ORDER BY destination_id",
                    (source_key,),
                )
            )

    def record_source_success(
        self,
        source_key: str,
        source_host: str,
        *,
        recovery_text: Callable[[dict[str, object], datetime], str],
        now: datetime | None = None,
    ) -> dict[str, object]:
        self.initialize()
        timestamp = self._utc_timestamp(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO schedule_source_health(source_key, source_host, status) "
                "VALUES (?, ?, 'HEALTHY') ON CONFLICT(source_key) DO NOTHING",
                (source_key, source_host),
            )
            previous = self._source_state(connection, source_key)
            assert previous is not None
            incident_id = previous["incident_id"]
            if incident_id is not None:
                # Callers serialize this with in-flight warning sends before the
                # transaction, so a successful send cannot be canceled unacknowledged.
                connection.execute(
                    "UPDATE admin_notification_outbox SET status = 'canceled' "
                    "WHERE incident_id = ? AND kind = 'warning' AND status = 'pending'",
                    (incident_id,),
                )
                received = connection.execute(
                    "SELECT destination_id FROM admin_notification_outbox "
                    "WHERE incident_id = ? AND kind = 'warning' AND status = 'sent'",
                    (incident_id,),
                ).fetchall()
                if received:
                    text = recovery_text(previous, datetime.fromisoformat(timestamp))
                    connection.executemany(
                        "INSERT INTO admin_notification_outbox"
                        "(incident_id, kind, destination_id, text, created_at, next_attempt_at) "
                        "VALUES (?, 'recovery', ?, ?, ?, ?) "
                        "ON CONFLICT(incident_id, kind, destination_id) DO NOTHING",
                        [
                            (incident_id, row[0], text, timestamp, timestamp)
                            for row in received
                        ],
                    )
                connection.execute(
                    "UPDATE schedule_outage_incidents SET recovered_at = ? WHERE id = ?",
                    (timestamp, incident_id),
                )
            connection.execute(
                "UPDATE schedule_source_health SET status = 'HEALTHY', consecutive_failures = 0, "
                "first_failure_at = NULL, incident_id = NULL, last_success_at = ? "
                "WHERE source_key = ?",
                (timestamp, source_key),
            )
            state = self._source_state(connection, source_key)
            assert state is not None
            connection.commit()
            return state

    def enqueue_admin_error(
        self, text: str, admin_ids: tuple[int, ...], *, now: datetime | None = None,
        dedupe_seconds: float = 0,
    ) -> int:
        self.initialize()
        timestamp = self._utc_timestamp(now)
        cutoff = (datetime.fromisoformat(timestamp) - timedelta(seconds=dedupe_seconds)).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            recipients = [admin_id for admin_id in set(admin_ids) if not dedupe_seconds or connection.execute(
                "SELECT 1 FROM admin_notification_outbox WHERE kind='critical' AND destination_id=? AND text=? "
                "AND (status='pending' OR created_at>?) LIMIT 1", (admin_id,text,cutoff)
            ).fetchone() is None]
            connection.executemany(
                "INSERT INTO admin_notification_outbox"
                "(kind, destination_id, text, created_at, next_attempt_at) "
                "VALUES ('critical', ?, ?, ?, ?)",
                [(admin_id, text, timestamp, timestamp) for admin_id in recipients],
            )
            connection.commit()
            return len(recipients)

    def pending_admin_notifications(
        self, *, limit: int = 100, now: datetime | None = None
    ) -> list[dict[str, object]]:
        self.initialize()
        with self._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT current.* FROM admin_notification_outbox AS current "
                    "WHERE current.status = 'pending' AND current.next_attempt_at <= ? "
                    "AND (current.kind = 'critical' OR NOT EXISTS "
                    "(SELECT 1 FROM admin_notification_outbox AS earlier "
                    "WHERE earlier.destination_id = current.destination_id "
                    "AND earlier.kind != 'critical' AND earlier.id < current.id "
                    "AND earlier.status = 'pending')) "
                    "ORDER BY current.id LIMIT ?",
                    (self._utc_timestamp(now), limit),
                )
            ]

    def admin_notification_is_sendable(
        self, delivery_id: int, *, now: datetime | None = None
    ) -> bool:
        self.initialize()
        with self._connect() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM admin_notification_outbox AS current "
                    "WHERE current.id = ? AND current.status = 'pending' "
                    "AND current.next_attempt_at <= ? AND (current.kind = 'critical' OR NOT EXISTS "
                    "(SELECT 1 FROM admin_notification_outbox AS earlier "
                    "WHERE earlier.destination_id = current.destination_id "
                    "AND earlier.kind != 'critical' AND earlier.id < current.id "
                    "AND earlier.status = 'pending'))",
                    (delivery_id, self._utc_timestamp(now)),
                ).fetchone()
                is not None
            )

    def mark_admin_notification_sent(
        self, delivery_id: int, *, now: datetime | None = None
    ) -> None:
        self.initialize()
        with self._connect() as connection:
            connection.execute(
                "UPDATE admin_notification_outbox SET status = 'sent', "
                "delivered_at = ?, last_error = NULL WHERE id = ? AND status = 'pending'",
                (self._utc_timestamp(now), delivery_id),
            )
            connection.commit()

    def mark_admin_notification_failed(
        self,
        delivery_id: int,
        error: str,
        *,
        permanent: bool = False,
        retry_after: float | None = None,
        now: datetime | None = None,
    ) -> None:
        self.initialize()
        timestamp = self._utc_timestamp(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT attempts FROM admin_notification_outbox "
                "WHERE id = ? AND status = 'pending'",
                (delivery_id,),
            ).fetchone()
            if row is None:
                return
            attempts = int(row["attempts"]) + 1
            # Persist the due timer; a process restart must not reset the backoff.
            delay = min(15 * 2 ** min(attempts - 1, 8), 3600)
            if retry_after is not None:
                delay = min(3600, max(1, retry_after))
            due = datetime.fromisoformat(timestamp) + timedelta(seconds=delay)
            connection.execute(
                "UPDATE admin_notification_outbox SET attempts = ?, status = ?, "
                "last_error = ?, next_attempt_at = ? WHERE id = ? AND status = 'pending'",
                (
                    attempts,
                    "failed" if permanent else "pending",
                    error[:500],
                    due.isoformat(),
                    delivery_id,
                ),
            )
            connection.commit()

    def find_import(self, sha256: str) -> ImportResult | None:
        """Return only persisted metadata; rejected files are never a cache hit."""
        self.initialize()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT i.id, i.source_filename, "
                "(SELECT COUNT(*) FROM lessons WHERE import_id = i.id) AS lesson_count "
                "FROM imports AS i WHERE i.sha256 = ?", (sha256,),
            ).fetchone()
        if row is None:
            return None
        return ImportResult(
            import_id=int(row["id"]), skipped=True, changes=(), parsed=None,
            source_filename=str(row["source_filename"]),
            stored_lesson_count=int(row["lesson_count"]),
        )

    def import_schedule(
        self,
        parsed: ParsedSchedule,
        *,
        sha256: str,
        source_url: str | None = None,
        approval_reason: str | None = None,
    ) -> ImportResult:
        self.initialize()
        if approval_reason is not None and not approval_reason.strip():
            raise ValueError("Approval requires a non-empty review reason")
        with self._connect() as connection:
            # Serialize both the SHA check and activation with other importers.
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT id FROM imports WHERE sha256 = ?", (sha256,)
            ).fetchone()
            if existing:
                connection.rollback()
                return ImportResult(
                    import_id=int(existing["id"]),
                    skipped=True,
                    changes=(),
                    parsed=parsed,
                )

            now = datetime.now(timezone.utc).isoformat()
            try:
                published_dates = {
                    date.fromisoformat(str(row["lesson_date"]))
                    for row in connection.execute(
                        "SELECT DISTINCT lesson_date FROM schedule_dates"
                    )
                }
                new_dates = tuple(sorted(set(parsed.dates) - published_dates))
                old_lessons = self._current_for_dates(connection, parsed.dates)
                previous_groups: dict[date, set[str]] = {}
                for row in connection.execute(
                    "SELECT d.lesson_date, i.coverage_json FROM schedule_dates d "
                    "JOIN imports i ON i.id = d.import_id "
                    "WHERE d.lesson_date BETWEEN ? AND ? ORDER BY i.id DESC",
                    (parsed.start_date.isoformat(), parsed.end_date.isoformat()),
                ):
                    day = date.fromisoformat(str(row["lesson_date"]))
                    if day not in previous_groups:
                        coverage = json.loads(str(row["coverage_json"]))
                        previous_groups[day] = set(coverage.get(day.isoformat(), ()))
                for lesson in old_lessons:
                    previous_groups.setdefault(lesson.lesson_date, set()).add(
                        lesson.group_name
                    )
                errors, anomalies = validate_import(
                    parsed, old_lessons, previous_groups, published_dates
                )
                if errors or (anomalies and approval_reason is None):
                    reasons = errors + anomalies
                    self._quarantine_import(
                        connection, parsed, sha256, source_url, reasons, now
                    )
                    connection.commit()
                    raise ImportQuarantinedError(reasons)
                from schedule_bot.matching import compare_lessons
                changes = compare_lessons(old_lessons, parsed.lessons)
                cursor = connection.execute(
                    """
                    INSERT INTO imports(
                        source_url, source_filename, sha256, imported_at,
                        start_date, end_date, warnings_json, coverage_json, approval_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source_url,
                        parsed.source_filename,
                        sha256,
                        now,
                        parsed.start_date.isoformat(),
                        parsed.end_date.isoformat(),
                        json.dumps(parsed.warnings + anomalies, ensure_ascii=False),
                        json.dumps(
                            {
                                day.isoformat(): groups
                                for day, groups in parsed.date_groups.items()
                            },
                            ensure_ascii=False,
                        ),
                        approval_reason,
                    ),
                )
                import_id = int(cursor.lastrowid)
                connection.execute(
                    "UPDATE imports SET empty_dates_json = ? WHERE id = ?",
                    (json.dumps([day.isoformat() for day in parsed.confirmed_empty_dates]), import_id),
                )

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
                        teacher, room, subgroup, raw_json, segments_json
                    ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                            json.dumps([part.to_dict() for part in lesson.segments], ensure_ascii=False),
                        )
                        for lesson in parsed.lessons
                    ),
                )
                self._enqueue_import_notifications(
                    connection, import_id, changes, new_dates, parsed.lessons, now
                )
                connection.executemany(
                    "INSERT INTO active_schedule_dates VALUES (?, ?, ?) "
                    "ON CONFLICT(lesson_date) DO UPDATE SET "
                    "source_import_id=excluded.source_import_id, effective_import_id=excluded.effective_import_id",
                    ((value, import_id, import_id) for value in date_values),
                )
                connection.execute(
                    "UPDATE quarantined_imports SET status = 'approved' WHERE sha256 = ?",
                    (sha256,),
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

    @staticmethod
    def _quarantine_import(
        connection: sqlite3.Connection,
        parsed: ParsedSchedule,
        sha256: str,
        source_url: str | None,
        reasons: tuple[str, ...],
        now: str,
    ) -> None:
        snapshot = {
            "dates": [day.isoformat() for day in parsed.dates],
            "date_groups": {
                day.isoformat(): groups for day, groups in parsed.date_groups.items()
            },
            "lessons": [lesson.to_dict() for lesson in parsed.lessons],
            "warnings": parsed.warnings,
            "validation_errors": parsed.validation_errors,
            "confirmed_empty_dates": [
                day.isoformat() for day in parsed.confirmed_empty_dates
            ],
            "start_date": parsed.start_date.isoformat(),
            "end_date": parsed.end_date.isoformat(),
        }
        connection.execute(
            """
            INSERT INTO quarantined_imports(
                sha256, source_filename, source_url, checked_at, reasons_json, snapshot_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(sha256) DO UPDATE SET
                checked_at = excluded.checked_at, reasons_json = excluded.reasons_json,
                snapshot_json = excluded.snapshot_json
            """,
            (
                sha256,
                parsed.source_filename,
                source_url,
                now,
                json.dumps(reasons, ensure_ascii=False),
                json.dumps(snapshot, ensure_ascii=False),
            ),
        )

    def _enqueue_import_notifications(
        self,
        connection: sqlite3.Connection,
        import_id: int,
        changes: tuple[ScheduleChange, ...],
        new_dates: tuple[date, ...],
        lessons: tuple[Lesson, ...],
        now: str,
    ) -> None:
        notifiable = tuple(change for change in changes if should_notify(change))
        if not notifiable:
            return
        chunks_by_profile: dict[tuple[str, str], tuple[str, ...]] = {}

        def deliveries() -> Iterator[Mapping[str, object]]:
            for profile in connection.execute(
                "SELECT user_id AS destination_id, 'user' AS destination_kind, "
                "'private' AS chat_type, role, target FROM user_profiles "
                "WHERE notifications = 1 UNION ALL "
                "SELECT chat_id, 'chat', chat_type, 'student', target FROM chat_profiles "
                "WHERE active = 1 AND notifications = 1"
            ):
                role, target = str(profile["role"]), str(profile["target"])
                key = (role, target)
                if key not in chunks_by_profile:
                    matching = changes_for_profile(notifiable, role=role, target=target)
                    chunks_by_profile[key] = tuple(
                        notification_chunks(
                            matching,
                            role=role,
                            target=target,
                            new_dates=new_dates,
                            lessons=lessons,
                        )
                    )
                for index, text in enumerate(chunks_by_profile[key]):
                    yield {
                        "import_id": import_id,
                        "destination_id": profile["destination_id"],
                        "destination_kind": profile["destination_kind"],
                        "chat_type": profile["chat_type"],
                        "chunk_index": index,
                        "text": text,
                    }

        self._insert_notifications(connection, deliveries(), now)

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
            lessons = [projected for lesson in lessons if (projected := lesson.for_teacher(target)) is not None]
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
                SELECT lesson_date FROM active_schedule_dates
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
        notifications: bool | None = None,
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
                    notifications = CASE WHEN ? IS NULL THEN user_profiles.notifications
                        ELSE excluded.notifications END,
                    updated_at = excluded.updated_at
                """,
                (
                    user_id,
                    role,
                    target,
                    int(notifications) if notifications is not None else 1,
                    datetime.now(timezone.utc).isoformat(),
                    int(notifications) if notifications is not None else None,
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

    def delete_user_data(self, user_id: int, actor_key: str | Iterable[str]) -> bool:
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
            actor_keys = tuple(dict.fromkeys((actor_key,) if isinstance(actor_key, str) else actor_key))
            placeholders = ",".join("?" for _ in actor_keys) or "NULL"
            events_cursor = connection.execute(
                f"DELETE FROM usage_events WHERE actor_key IN ({placeholders})", actor_keys
            )
            outbox_cursor = connection.execute(
                "DELETE FROM notification_outbox "
                "WHERE destination_kind = 'user' AND destination_id = ?",
                (user_id,),
            )
            connection.commit()
            return any(
                cursor.rowcount > 0
                for cursor in (
                    profile_cursor,
                    chat_cursor,
                    events_cursor,
                    outbox_cursor,
                )
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
                    last_pinned_schedule_date = CASE
                        WHEN target != excluded.target THEN NULL
                        ELSE last_pinned_schedule_date END,
                    last_pinned_schedule_hash = CASE
                        WHEN target != excluded.target THEN NULL
                        ELSE last_pinned_schedule_hash END,
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
            pending_pin_message_id=None,
            pending_pin_schedule_date=None,
            pending_pin_schedule_hash=None,
            pin_retry_after=None,
            pin_attempts=0,
        )

    def set_chat_pending_pin(
        self,
        chat_id: int,
        *,
        message_id: int,
        schedule_date: date | None,
        schedule_hash: str | None,
    ) -> None:
        self._update_chat_profile(
            chat_id,
            pending_pin_message_id=message_id,
            pending_pin_schedule_date=(
                schedule_date.isoformat() if schedule_date is not None else None
            ),
            pending_pin_schedule_hash=schedule_hash,
            pin_retry_after=None,
            pin_attempts=0,
        )

    def defer_chat_pin(self, chat_id: int, *, now: datetime) -> None:
        self.initialize()
        timestamp = datetime.fromisoformat(self._utc_timestamp(now))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT pin_attempts FROM chat_profiles WHERE chat_id = ?", (chat_id,)
            ).fetchone()
            if row is None:
                return
            attempts = int(row["pin_attempts"]) + 1
            # Five minutes initially, capped at one hour for persistent failures.
            delay_seconds = min(300 * 2 ** min(attempts - 1, 4), 3600)
            connection.execute(
                "UPDATE chat_profiles SET pin_attempts = ?, pin_retry_after = ?, "
                "updated_at = ? WHERE chat_id = ?",
                (
                    attempts,
                    (timestamp + timedelta(seconds=delay_seconds)).isoformat(),
                    self._utc_timestamp(None),
                    chat_id,
                ),
            )
            connection.commit()

    def clear_chat_pending_pin(self, chat_id: int) -> None:
        self._update_chat_profile(
            chat_id,
            pending_pin_message_id=None,
            pending_pin_schedule_date=None,
            pending_pin_schedule_hash=None,
            pin_retry_after=None,
            pin_attempts=0,
        )

    def clear_chat_last_pin(self, chat_id: int) -> None:
        self.set_chat_last_pin(
            chat_id, message_id=None, schedule_date=None, schedule_hash=None
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
        *,
        now: datetime | None = None,
    ) -> None:
        self.initialize()
        timestamp = self._utc_timestamp(now)
        with self._connect() as connection:
            self._insert_notifications(connection, deliveries, timestamp)
            connection.commit()

    @staticmethod
    def _insert_notifications(
        connection: sqlite3.Connection,
        deliveries: Iterable[Mapping[str, object]],
        now: str,
    ) -> None:
        expires_at = (
            datetime.fromisoformat(now) + ScheduleRepository.NOTIFICATION_TTL
        ).isoformat()
        rows = (
            (
                int(item["import_id"]),
                int(item["destination_id"]),
                str(item["destination_kind"]),
                str(item["chat_type"]),
                int(item["chunk_index"]),
                str(item["text"]),
                int(bool(item.get("pin_requested", False))),
                now,
                now,
                expires_at,
                int(item["destination_id"]),
                str(item["destination_kind"]),
            )
            for item in deliveries
        )
        connection.executemany(
            """
            INSERT INTO notification_outbox(
                import_id, destination_id, destination_kind, chat_type,
                chunk_index, text, pin_requested, created_at, next_attempt_at, expires_at,
                target_snapshot
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                (SELECT target FROM chat_profiles WHERE chat_id = ? AND ? = 'chat'))
            ON CONFLICT(import_id, destination_id, destination_kind, chunk_index)
            DO NOTHING
            """,
            rows,
        )

    def pending_notifications(
        self,
        *,
        limit: int = 100,
        now: datetime | None = None,
    ) -> list[dict[str, object]]:
        self.initialize()
        timestamp = self._utc_timestamp(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_notifications(connection, timestamp)
            self._block_notification_tails(connection)
            rows = connection.execute(
                """
                SELECT current.* FROM notification_outbox AS current
                WHERE current.status = 'pending' AND current.next_attempt_at <= ?
                  AND NOT EXISTS (
                      SELECT 1 FROM notification_outbox AS earlier
                      WHERE earlier.import_id = current.import_id
                        AND earlier.destination_id = current.destination_id
                        AND earlier.destination_kind = current.destination_kind
                        AND earlier.chunk_index < current.chunk_index
                        AND earlier.status != 'sent'
                  )
                ORDER BY current.import_id, current.chunk_index, current.id
                LIMIT ?
                """,
                (timestamp, limit),
            ).fetchall()
            connection.commit()
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
                "next_attempt_at": str(row["next_attempt_at"]),
                "expires_at": str(row["expires_at"]),
                "failure_kind": row["failure_kind"],
            }
            for row in rows
        ]

    def mark_notification_sent(self, delivery_id: int) -> None:
        self.initialize()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE notification_outbox
                SET status = 'sent', delivered_at = ?, last_error = NULL,
                    failure_kind = NULL
                WHERE id = ? AND status = 'pending'
                """,
                (datetime.now(timezone.utc).isoformat(), delivery_id),
            )
            connection.commit()

    def mark_notification_failed(
        self,
        delivery_id: int,
        error: str,
        *,
        permanent: bool = False,
        retry_after: float | None = None,
        now: datetime | None = None,
    ) -> None:
        self.initialize()
        timestamp = self._utc_timestamp(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT attempts, expires_at FROM notification_outbox "
                "WHERE id = ? AND status = 'pending'",
                (delivery_id,),
            ).fetchone()
            if row is None:
                return
            attempts = int(row["attempts"]) + 1
            if retry_after is None:
                delay = min(15 * 2 ** min(attempts - 1, 9), 3600)
                delay = min(delay * random.uniform(0.8, 1.2), 3600)
            else:
                delay = max(1.0, retry_after)
            due = datetime.fromisoformat(timestamp) + timedelta(seconds=delay)
            expired = (
                row["expires_at"] is not None and str(row["expires_at"]) <= timestamp
            )
            connection.execute(
                """
                UPDATE notification_outbox
                SET attempts = ?, status = ?, last_error = ?,
                    next_attempt_at = ?, failure_kind = ?
                WHERE id = ?
                """,
                (
                    attempts,
                    "failed" if permanent or expired else "pending",
                    error[:500],
                    due.isoformat(),
                    "expired"
                    if expired
                    else ("permanent" if permanent else "transient"),
                    delivery_id,
                ),
            )
            self._block_notification_tails(connection)
            connection.commit()

    @staticmethod
    def _block_notification_tails(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            UPDATE notification_outbox AS current
            SET status = 'failed', failure_kind = 'blocked',
                last_error = 'Previous notification chunk failed'
            WHERE current.status = 'pending' AND EXISTS (
                SELECT 1 FROM notification_outbox AS earlier
                WHERE earlier.import_id = current.import_id
                    AND earlier.destination_id = current.destination_id
                    AND earlier.destination_kind = current.destination_kind
                    AND earlier.chunk_index < current.chunk_index
                    AND earlier.status = 'failed'
            )
            """
        )

    @staticmethod
    def _expire_notifications(connection: sqlite3.Connection, timestamp: str) -> None:
        connection.execute(
            """
            UPDATE notification_outbox AS current
            SET status = 'failed', failure_kind = 'expired',
                last_error = 'Notification delivery expired'
            WHERE current.status = 'pending' AND EXISTS (
                SELECT 1 FROM notification_outbox AS expired
                WHERE expired.import_id = current.import_id
                    AND expired.destination_id = current.destination_id
                    AND expired.destination_kind = current.destination_kind
                    AND expired.status = 'pending' AND expired.expires_at <= ?
            )
            """,
            (timestamp,),
        )

    def notification_is_sendable(
        self, delivery_id: int, *, now: datetime | None = None
    ) -> bool:
        """Recheck a queued candidate after waits, including subscription consent."""
        self.initialize()
        timestamp = self._utc_timestamp(now)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM notification_outbox WHERE id = ?",
                (delivery_id,),
            ).fetchone()
            if row is None or row["status"] != "pending":
                return False
            if str(row["next_attempt_at"]) > timestamp or (
                row["expires_at"] is not None and str(row["expires_at"]) <= timestamp
            ):
                return False
            if row["destination_kind"] == "user":
                allowed = connection.execute(
                    "SELECT 1 FROM user_profiles WHERE user_id = ? AND notifications = 1",
                    (row["destination_id"],),
                ).fetchone()
            else:
                allowed = connection.execute(
                    "SELECT 1 FROM chat_profiles WHERE chat_id = ? "
                    "AND notifications = 1 AND active = 1",
                    (row["destination_id"],),
                ).fetchone()
            if not allowed:
                connection.execute(
                    "UPDATE notification_outbox SET status = 'failed', "
                    "failure_kind = 'cancelled', last_error = 'Subscription unavailable' "
                    "WHERE destination_id = ? AND destination_kind = ? AND status = 'pending'",
                    (row["destination_id"], row["destination_kind"]),
                )
                connection.commit()
                return False
            return (
                connection.execute(
                    "SELECT 1 FROM notification_outbox WHERE import_id = ? AND destination_id = ? "
                    "AND destination_kind = ? AND chunk_index < ? AND status != 'sent' LIMIT 1",
                    (
                        row["import_id"],
                        row["destination_id"],
                        row["destination_kind"],
                        row["chunk_index"],
                    ),
                ).fetchone()
                is None
            )

    def redrive_notifications(
        self,
        import_id: int,
        destination_id: int,
        destination_kind: Literal["user", "chat"],
        *,
        now: datetime | None = None,
    ) -> int:
        """Retry an entire unsent series after review, preserving successful chunks."""
        self.initialize()
        timestamp = self._utc_timestamp(now)
        expires = datetime.fromisoformat(timestamp) + self.NOTIFICATION_TTL
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if destination_kind == "user":
                allowed = connection.execute(
                    "SELECT 1 FROM user_profiles WHERE user_id = ? AND notifications = 1",
                    (destination_id,),
                ).fetchone()
            elif destination_kind == "chat":
                allowed = connection.execute(
                    "SELECT 1 FROM chat_profiles WHERE chat_id = ? AND active = 1 AND notifications = 1",
                    (destination_id,),
                ).fetchone()
            else:
                raise ValueError("Invalid destination kind")
            if not allowed:
                raise ValueError("Destination has no active notification subscription")
            cursor = connection.execute(
                "UPDATE notification_outbox SET status = 'pending', attempts = 0, "
                "next_attempt_at = ?, expires_at = ?, failure_kind = NULL, last_error = NULL "
                "WHERE import_id = ? AND destination_id = ? AND destination_kind = ? "
                "AND status != 'sent'",
                (
                    timestamp,
                    expires.isoformat(),
                    import_id,
                    destination_id,
                    destination_kind,
                ),
            )
            connection.commit()
            return cursor.rowcount

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

        def property_value(payload: str, name: str):
            # Match the previous treatment of malformed JSON and non-object JSON.
            try:
                properties = json.loads(payload)
            except (json.JSONDecodeError, TypeError):
                properties = {}
            if not isinstance(properties, dict):
                return None
            if name == "count_user":
                return int(properties.get("_actor_kind") in (None, "user"))
            value = properties.get(name)
            return value if isinstance(value, str) and value else None

        timestamp = self._utc_timestamp(since)
        with self._connect() as connection:
            connection.create_function("usage_property", 2, property_value, deterministic=True)
            # SQLite can spill grouping/sorting to disk instead of retaining all
            # raw event rows and actor identifiers in Python.
            connection.execute("PRAGMA temp_store = FILE")
            actors = int(connection.execute(
                "SELECT COUNT(DISTINCT actor_key) FROM usage_events "
                "WHERE timestamp >= ? AND usage_property(properties_json, 'count_user') = 1",
                (timestamp,),
            ).fetchone()[0])
            events_by_type = {
                str(row[0]): int(row[1]) for row in connection.execute(
                    "SELECT event_type, COUNT(*) FROM usage_events "
                    "WHERE timestamp >= ? GROUP BY event_type", (timestamp,),
                )
            }

            def property_counts(name: str) -> dict[str, int]:
                return {
                    str(row[0]): int(row[1]) for row in connection.execute(
                        "SELECT usage_property(properties_json, ?) AS value, COUNT(*) "
                        "FROM usage_events WHERE timestamp >= ? "
                        "GROUP BY value HAVING value IS NOT NULL", (name, timestamp),
                    )
                }

            schedule_scopes = property_counts("scope")
            schedule_results = property_counts("result")
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

        return {
            "active_actors": actors,
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
            "pending_pin_message_id",
            "pending_pin_schedule_date",
            "pending_pin_schedule_hash",
            "pin_retry_after",
            "pin_attempts",
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
            "pending_pin_message_id": (
                int(row["pending_pin_message_id"])
                if row["pending_pin_message_id"] is not None
                else None
            ),
            "pending_pin_schedule_date": (
                date.fromisoformat(str(row["pending_pin_schedule_date"]))
                if row["pending_pin_schedule_date"] is not None
                else None
            ),
            "pending_pin_schedule_hash": (
                str(row["pending_pin_schedule_hash"])
                if row["pending_pin_schedule_hash"] is not None
                else None
            ),
            "pin_retry_after": (
                datetime.fromisoformat(str(row["pin_retry_after"]))
                if row["pin_retry_after"] is not None
                else None
            ),
            "pin_attempts": int(row["pin_attempts"]),
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
        from schedule_bot.matching import compare_lessons
        return compare_lessons(before.values(), after.values())

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
                int(row["lesson_number"]) if row["lesson_number"] is not None else None
            ),
            start_time=row["start_time"],
            end_time=row["end_time"],
            subject=str(row["subject"]),
            teacher=row["teacher"],
            room=row["room"],
            subgroup=row["subgroup"],
            raw_data=json.loads(str(row["raw_json"])),
            segments=tuple(LessonSegment(**part) for part in json.loads(str(row["segments_json"]))),
        )
