from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from scripts.backup_tools import copy_database, fingerprint, restore_stopped_database


def make_database(path: Path, value: str = "original", version: int = 7) -> None:
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            connection.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT)")
            connection.execute("INSERT INTO records VALUES (1, ?)", (value,))
            connection.execute(f"PRAGMA user_version={version}")


def test_restore_drill_is_verified_and_cannot_replace_existing_data(tmp_path: Path) -> None:
    source, restored = tmp_path / "source.db", tmp_path / "isolated" / "restored.db"
    make_database(source)
    before = source.read_bytes()
    assert copy_database(source, restored) == fingerprint(source)
    assert fingerprint(restored) == fingerprint(source)
    assert source.read_bytes() == before
    with pytest.raises(ValueError, match="new database"):
        copy_database(source, restored)


def test_backup_includes_uncheckpointed_wal_transactions(tmp_path: Path) -> None:
    source, destination = tmp_path / "wal.db", tmp_path / "backup.db"
    make_database(source)
    with sqlite3.connect(source) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("INSERT INTO records VALUES (2, 'committed in WAL')")
        connection.commit()
        assert Path(str(source) + "-wal").exists()
        assert copy_database(source, destination)["tables"]["records"]["count"] == 2
        assert destination.read_bytes()[18:20] == b"\x01\x01"
        assert not list(tmp_path.glob(".restore-*-wal"))
        assert not list(tmp_path.glob(".restore-*-shm"))


def test_corrupt_database_never_publishes_restore(tmp_path: Path) -> None:
    source, destination = tmp_path / "corrupt.db", tmp_path / "target.db"
    source.write_bytes(b"not sqlite")
    with pytest.raises(sqlite3.Error):
        copy_database(source, destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".restore-*"))


def test_foreign_key_violations_are_rejected(tmp_path: Path) -> None:
    source = tmp_path / "invalid.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE child (parent_id INTEGER REFERENCES parent(id))")
        connection.execute("INSERT INTO child VALUES (99)")
    with pytest.raises(ValueError, match="foreign key"):
        fingerprint(source)


def test_stopped_restore_preserves_failed_database_and_restores_old_schema(tmp_path: Path) -> None:
    source, current, preserved = (tmp_path / name for name in ("before.db", "working.db", "failed.db"))
    make_database(source, "old data", 7)
    make_database(current, "new data", 9)
    old, new = fingerprint(source), fingerprint(current)
    assert restore_stopped_database(source, current, preserved) == old
    assert fingerprint(current) == old
    assert fingerprint(preserved) == new
    assert not list(tmp_path.glob("*.restore-candidate"))


def test_missing_backup_does_not_create_empty_database(tmp_path: Path) -> None:
    source, destination = tmp_path / "absent.db", tmp_path / "target.db"
    with pytest.raises(ValueError, match="does not exist"):
        copy_database(source, destination)
    assert not source.exists() and not destination.exists()


def test_corrupt_failed_database_does_not_block_verified_snapshot_rollback(tmp_path: Path) -> None:
    source, current, preserved = (tmp_path / name for name in ("before.db", "working.db", "failed.db"))
    make_database(source)
    current.write_bytes(b"corrupt failed deployment")
    assert restore_stopped_database(source, current, preserved) == fingerprint(source)
    assert preserved.read_bytes() == b"corrupt failed deployment"
