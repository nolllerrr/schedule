from __future__ import annotations

from datetime import datetime
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
