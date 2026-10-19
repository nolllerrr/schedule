import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from schedule_bot.delivery_audit import begin_attempt, record_attempt, review_attempt
from schedule_bot.domain import Lesson, ParsedSchedule
from schedule_bot.maintenance import cleanup_technical_data
from schedule_bot.repository import ScheduleRepository


NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=100)


def repository(tmp_path):
    repo = ScheduleRepository(tmp_path / "retention.db")
    day = date(2026, 10, 17)
    lesson = Lesson(day, "ИС-22", 1, "1 пара", 1, "09:00", "10:30", "Synthetic lesson")
    repo.import_schedule(
        ParsedSchedule("17.10.2026.xlsx", day, day, (day,), (lesson,)), sha256="a" * 64,
    )
    return repo


def series(repo, destination, *, states=("sent", "sent"), timestamp=OLD):
    repo.enqueue_notifications([
        {"import_id": 1, "destination_id": destination, "destination_kind": "user",
         "chat_type": "private", "chunk_index": index, "text": f"synthetic {index}"}
        for index, _ in enumerate(states)
    ], now=timestamp)
    with repo._connect() as connection:
        for index, state in enumerate(states):
            connection.execute(
                "UPDATE notification_outbox SET status=?, delivered_at=? "
                "WHERE destination_id=? AND chunk_index=?",
                (state, timestamp.isoformat() if state == "sent" else None, destination, index),
            )
        connection.commit()
        return [row[0] for row in connection.execute(
            "SELECT id FROM notification_outbox WHERE destination_id=? ORDER BY chunk_index", (destination,),
        )]


def table_count(repo, table):
    with repo._connect() as connection:
        return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def downloaded(root: Path, digest: str, *, timestamp=OLD, name="17.10.2026.xlsx"):
    root.mkdir(exist_ok=True)
    path = root / f"{digest[:12]}_{name}"
    path.write_bytes(b"synthetic workbook")
    os.utime(path, (timestamp.timestamp(), timestamp.timestamp()))
    return path


def test_cleanup_defaults_to_dry_run_and_preserves_whole_pending_series(tmp_path):
    repo = repository(tmp_path)
    removed_ids = series(repo, 101, states=("sent", "failed"))
    series(repo, 102, states=("sent", "pending"))
    series(repo, 103, timestamp=NOW)
    attempt = begin_attempt(repo, notification_id=removed_ids[0], now=OLD)
    record_attempt(repo, attempt, "sent", now=OLD)
    file = downloaded(tmp_path / "downloads", "d" * 64)
    dry = cleanup_technical_data(repo, file.parent, now=NOW)
    assert dry["dry_run"]
    assert dry["notification_rows"] == 2 and dry["attempt_rows"] == 1
    assert dry["download_files"] == 1
    assert table_count(repo, "notification_outbox") == 6 and file.exists()
    before_history = [table_count(repo, table) for table in ("imports", "lessons", "schedule_dates")]
    actual = cleanup_technical_data(repo, file.parent, now=NOW, dry_run=False)
    assert actual == {**dry, "dry_run": False}
    assert table_count(repo, "notification_outbox") == 4
    assert table_count(repo, "delivery_attempts") == 0
    assert not file.exists()
    assert [table_count(repo, table) for table in ("imports", "lessons", "schedule_dates")] == before_history


@pytest.mark.parametrize("outcome", ["uncertain", "accepted"])
def test_unreviewed_delivery_uncertainty_blocks_whole_series(tmp_path, outcome):
    repo = repository(tmp_path)
    ids = series(repo, 101)
    attempt = begin_attempt(repo, notification_id=ids[0], now=OLD)
    record_attempt(repo, attempt, outcome, now=OLD)
    result = cleanup_technical_data(repo, tmp_path / "downloads", now=NOW, dry_run=False)
    assert result["notification_rows"] == 0
    assert table_count(repo, "notification_outbox") == 2
    assert review_attempt(repo, attempt, "Synthetic uncertainty examined", now=NOW)
    result = cleanup_technical_data(repo, tmp_path / "downloads", now=NOW, dry_run=False)
    assert result["notification_rows"] == 2
    assert table_count(repo, "delivery_attempts") == 0


def test_download_cleanup_keeps_active_latest_source_quarantine_young_and_unknown_files(tmp_path):
    repo = repository(tmp_path)
    root = tmp_path / "downloads"
    active = downloaded(root, "a" * 64)
    latest_source = downloaded(root, "b" * 64)
    quarantine = downloaded(root, "c" * 64)
    obsolete = downloaded(root, "d" * 64)
    young = downloaded(root, "e" * 64, timestamp=NOW)
    unknown = root / "manual-source.xlsx"
    unknown.write_bytes(b"manual input")
    part = root / "unfinished.part"
    part.write_bytes(b"in progress")
    os.utime(unknown, (OLD.timestamp(), OLD.timestamp()))
    os.utime(part, (OLD.timestamp(), OLD.timestamp()))
    with repo._connect() as connection:
        cursor = connection.execute(
            "INSERT INTO imports(source_filename,sha256,imported_at,start_date,end_date) VALUES(?,?,?,?,?)",
            ("17.10.2026.xlsx", "b" * 64, OLD.isoformat(), "2026-10-17", "2026-10-17"),
        )
        connection.execute(
            "INSERT INTO source_publications VALUES(?,?,?,?,?,?,?)",
            ("source-key", "https://source.test/file.xlsx", cursor.lastrowid,
             "2026-10-17", "2026-10-17", OLD.isoformat(), 0),
        )
        connection.execute(
            "INSERT INTO quarantined_imports(sha256,source_filename,checked_at,reasons_json,snapshot_json) "
            "VALUES(?,?,?,?,?)", ("c" * 64, "17.10.2026.xlsx", OLD.isoformat(), "[]", "{}"),
        )
        connection.commit()
    result = cleanup_technical_data(repo, root, now=NOW, dry_run=False)
    assert result["download_files"] == 1
    assert not obsolete.exists()
    assert all(path.exists() for path in (active, latest_source, quarantine, young, unknown, part))


def test_cleanup_batch_limit_counts_series_without_partial_deletion(tmp_path):
    repo = repository(tmp_path)
    series(repo, 101, states=("sent", "sent", "sent"))
    series(repo, 102, states=("sent", "sent", "sent"))
    result = cleanup_technical_data(repo, tmp_path / "downloads", now=NOW, dry_run=False, limit=1)
    assert result["notification_rows"] == 3
    assert table_count(repo, "notification_outbox") == 3
    with repo._connect() as connection:
        assert connection.execute("SELECT COUNT(DISTINCT destination_id) FROM notification_outbox").fetchone()[0] == 1


def test_admin_cleanup_preserves_active_incidents_and_pending_recovery(tmp_path):
    repo = repository(tmp_path)
    with repo._connect() as connection:
        connection.execute(
            "INSERT INTO schedule_source_health(source_key,source_host,status) VALUES('s','source.test','DOWN')"
        )
        for incident in (1, 2, 3):
            connection.execute(
                "INSERT INTO schedule_outage_incidents(id,source_key,started_at,recovered_at) VALUES(?,?,?,?)",
                (incident, "s", OLD.isoformat(), None if incident == 1 else OLD.isoformat()),
            )
        items = [(1, "warning", "sent"), (2, "warning", "sent"), (2, "recovery", "sent"),
                 (3, "warning", "sent"), (3, "recovery", "pending"), (None, "critical", "failed")]
        connection.executemany(
            "INSERT INTO admin_notification_outbox(incident_id,kind,destination_id,text,status,created_at,next_attempt_at,delivered_at) "
            "VALUES(?,?,123,'synthetic',?,?,?,?)",
            [(incident, kind, state, OLD.isoformat(), OLD.isoformat(), OLD.isoformat() if state == "sent" else None)
             for incident, kind, state in items],
        )
        connection.commit()
    result = cleanup_technical_data(repo, tmp_path / "downloads", now=NOW, dry_run=False)
    assert result["admin_rows"] == 3
    with repo._connect() as connection:
        assert sorted(row[0] for row in connection.execute("SELECT incident_id FROM admin_notification_outbox")) == [1, 3, 3]


def test_admin_cleanup_preserves_unreviewed_accepted_attempt(tmp_path):
    repo = repository(tmp_path)
    repo.enqueue_admin_error("Synthetic alert", (123,), now=OLD)
    with repo._connect() as connection:
        connection.execute("UPDATE admin_notification_outbox SET status='sent',delivered_at=?", (OLD.isoformat(),))
        connection.commit()
    attempt = begin_attempt(repo, admin_notification_id=1, now=OLD)
    record_attempt(repo, attempt, "accepted", telegram_message_id=55, now=OLD)
    assert cleanup_technical_data(repo, tmp_path / "downloads", now=NOW)["admin_rows"] == 0


def test_download_cleanup_does_not_follow_file_symlink(tmp_path):
    repo = repository(tmp_path)
    root = tmp_path / "downloads"
    root.mkdir()
    external = tmp_path / "outside.xlsx"
    external.write_bytes(b"outside")
    os.utime(external, (OLD.timestamp(), OLD.timestamp()))
    link = root / f"{'f' * 12}_17.10.2026.xlsx"
    try:
        link.symlink_to(external)
    except OSError:
        pytest.skip("Creating symlinks is unavailable on this host")
    result = cleanup_technical_data(repo, root, now=NOW, dry_run=False)
    assert result["download_files"] == 0
    assert external.exists() and link.is_symlink()


@pytest.mark.parametrize("field,value", [("outbox_retention_days", 0), ("downloads_retention_days", -1), ("limit", 0), ("limit", True)])
def test_invalid_retention_budgets_are_rejected(tmp_path, field, value):
    repo = repository(tmp_path)
    with pytest.raises(ValueError):
        cleanup_technical_data(repo, tmp_path / "downloads", **{field: value})
