from __future__ import annotations

import os
import re
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

from schedule_bot.repository import ScheduleRepository


def create_database_backup(
    repository: ScheduleRepository,
    backup_directory: str | Path,
    *,
    retention_count: int = 14,
    now: datetime | None = None,
) -> Path:
    """Create a verified backup and keep only the newest requested copies."""
    if retention_count < 1:
        raise ValueError("retention_count must be at least 1")

    backup_path = Path(backup_directory)
    timestamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S-%f")
    destination = backup_path / f"schedule-{timestamp}.db"
    repository.backup_to(destination)

    backups = sorted(
        backup_path.glob("schedule-*.db"),
        key=lambda item: (item.stat().st_mtime_ns, item.name),
        reverse=True,
    )
    for expired in backups[retention_count:]:
        expired.unlink()
    return destination


def backup_before_migration(
    repository: ScheduleRepository,
    backup_directory: str | Path,
    *,
    now: datetime | None = None,
) -> Path | None:
    """Preserve the old database once before applying a newer schema."""
    current_version = repository.current_schema_version()
    if current_version == 0 or current_version >= repository.SCHEMA_VERSION:
        return None

    backup_path = Path(backup_directory)
    timestamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S-%f")
    destination = backup_path / (
        f"pre-migration-v{current_version}-to-v{repository.SCHEMA_VERSION}-"
        f"{timestamp}.db"
    )
    return repository.backup_to(destination, initialize=False)


def cleanup_technical_data(
    repository: ScheduleRepository, downloads_path: str | Path, *,
    outbox_retention_days: int = 90, downloads_retention_days: int = 90,
    dry_run: bool = True, limit: int = 100, now: datetime | None = None,
) -> dict[str, int | bool]:
    """Prune old terminal series and expendable downloads in bounded batches.

    Schedule imports/lessons, current source files, pending quarantines and
    unreviewed uncertainty survive. A batch contains at most ``limit`` series
    and ``limit`` files; a multipart series is always removed as a whole.
    In-progress .part files and all unrecognized names are intentionally left.
    """
    for value in (outbox_retention_days, downloads_retention_days, limit):
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError("Retention periods and batch limit must be positive integers")
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    outbox_cutoff = (moment - timedelta(days=outbox_retention_days)).isoformat()
    file_cutoff = (moment - timedelta(days=downloads_retention_days)).timestamp()
    result: dict[str, int | bool] = {
        "dry_run": dry_run, "notification_rows": 0, "admin_rows": 0,
        "attempt_rows": 0, "download_files": 0, "download_bytes": 0,
    }
    repository.initialize()
    with repository._connect() as connection:
        connection.execute("BEGIN" if dry_run else "BEGIN IMMEDIATE")
        user_series = connection.execute(
            "SELECT n.import_id,n.destination_id,n.destination_kind,COUNT(*) AS row_count "
            "FROM notification_outbox n GROUP BY n.import_id,n.destination_id,n.destination_kind "
            "HAVING SUM(CASE WHEN n.status='pending' THEN 1 ELSE 0 END)=0 "
            "AND MAX(COALESCE(n.delivered_at,n.created_at)) < ? "
            "AND NOT EXISTS (SELECT 1 FROM delivery_attempts a "
            "JOIN notification_outbox q ON q.id=a.notification_id "
            "WHERE q.import_id=n.import_id AND q.destination_id=n.destination_id "
            "AND q.destination_kind=n.destination_kind "
            "AND a.outcome IN ('uncertain','accepted') AND a.reviewed_at IS NULL) "
            "ORDER BY MAX(COALESCE(n.delivered_at,n.created_at)),MIN(n.id) LIMIT ?",
            (outbox_cutoff, limit),
        ).fetchall()
        for series in user_series:
            parameters = (series["import_id"], series["destination_id"], series["destination_kind"])
            predicate = "import_id=? AND destination_id=? AND destination_kind=?"
            attempts = connection.execute(
                "SELECT COUNT(*) FROM delivery_attempts WHERE notification_id IN "
                f"(SELECT id FROM notification_outbox WHERE {predicate})", parameters,
            ).fetchone()[0]
            result["notification_rows"] += int(series["row_count"])
            result["attempt_rows"] += int(attempts)
            if not dry_run:
                connection.execute(f"DELETE FROM notification_outbox WHERE {predicate}", parameters)

        remaining = limit - len(user_series)
        admin_series = connection.execute(
            "SELECT n.incident_id,n.destination_id,"
            "CASE WHEN n.incident_id IS NULL THEN n.id ELSE 0 END AS critical_id,"
            "COUNT(*) AS row_count FROM admin_notification_outbox n "
            "GROUP BY n.incident_id,n.destination_id,critical_id "
            "HAVING SUM(CASE WHEN n.status='pending' THEN 1 ELSE 0 END)=0 "
            "AND MAX(COALESCE(n.delivered_at,n.created_at)) < ? "
            "AND (n.incident_id IS NULL OR NOT EXISTS "
            "(SELECT 1 FROM schedule_outage_incidents i WHERE i.id=n.incident_id AND i.recovered_at IS NULL)) "
            "AND NOT EXISTS (SELECT 1 FROM delivery_attempts a "
            "JOIN admin_notification_outbox q ON q.id=a.admin_notification_id "
            "WHERE q.destination_id=n.destination_id "
            "AND (q.incident_id=n.incident_id OR (n.incident_id IS NULL AND q.id=n.id)) "
            "AND a.outcome IN ('uncertain','accepted') AND a.reviewed_at IS NULL) "
            "ORDER BY MAX(COALESCE(n.delivered_at,n.created_at)),MIN(n.id) LIMIT ?",
            (outbox_cutoff, remaining),
        ).fetchall()
        for series in admin_series:
            if series["incident_id"] is None:
                predicate, parameters = "id=?", (series["critical_id"],)
            else:
                predicate = "incident_id=? AND destination_id=?"
                parameters = (series["incident_id"], series["destination_id"])
            attempts = connection.execute(
                "SELECT COUNT(*) FROM delivery_attempts WHERE admin_notification_id IN "
                f"(SELECT id FROM admin_notification_outbox WHERE {predicate})", parameters,
            ).fetchone()[0]
            result["admin_rows"] += int(series["row_count"])
            result["attempt_rows"] += int(attempts)
            if not dry_run:
                connection.execute(f"DELETE FROM admin_notification_outbox WHERE {predicate}", parameters)

        protected = {
            str(row[0])[:12].lower() for row in connection.execute(
                "SELECT sha256 FROM imports WHERE id IN "
                "(SELECT import_id FROM source_publications "
                "UNION SELECT source_import_id FROM active_schedule_dates "
                "UNION SELECT effective_import_id FROM active_schedule_dates) "
                "UNION SELECT sha256 FROM quarantined_imports WHERE status='pending'"
            )
        }
        # Keep the database write lock through unlink: newly activated snapshots
        # cannot become unprotected between this query and the filesystem pass.
        root = Path(downloads_path)
        if root.exists() and not root.is_symlink() and not root.is_junction():
            resolved_root = root.resolve()
            count = 0
            with os.scandir(resolved_root) as entries:
                for entry in entries:
                    if count >= limit:
                        break
                    match = re.fullmatch(r"([a-fA-F0-9]{12})_.+\.xlsx", entry.name, re.IGNORECASE)
                    if match is None or match[1].lower() in protected or entry.is_symlink():
                        continue
                    try:
                        path = Path(entry.path)
                        # Windows DirEntry.stat may report st_ino=0; compare
                        # two lstat calls instead of mixing the two APIs.
                        info = path.lstat()
                        if not stat.S_ISREG(info.st_mode) or info.st_mtime >= file_cutoff:
                            continue
                        if path.resolve().parent != resolved_root:
                            continue
                        # Re-check after candidate selection: the downloader
                        # refreshes mtime whenever a cached file is used again.
                        current = path.lstat()
                        if (
                            not stat.S_ISREG(current.st_mode) or current.st_mtime >= file_cutoff
                            or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)
                        ):
                            continue
                        if not dry_run:
                            path.unlink()
                    except (FileNotFoundError, PermissionError):
                        continue
                    result["download_files"] += 1
                    result["download_bytes"] += current.st_size
                    count += 1
        if dry_run:
            connection.rollback()
        else:
            connection.commit()
    return result
