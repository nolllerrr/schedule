from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import backup_tools
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
    with closing(sqlite3.connect(source)) as connection, connection:
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
    with closing(sqlite3.connect(source)) as connection, connection:
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


@pytest.fixture
def stopped_wal_database(tmp_path):
    """Clone a stopped v9 database whose latest delivery state exists only in WAL."""
    from schedule_bot.repository import ScheduleRepository

    source, live, current, preserved = (
        tmp_path / name for name in ("snapshot.db", "live.db", "working.db", "preserved.db")
    )
    timestamp = "2026-10-27T08:00:00+00:00"
    with closing(sqlite3.connect(live)) as connection, connection:
        connection.execute("PRAGMA foreign_keys=ON")
        for version in range(1, 10):
            getattr(ScheduleRepository, f"_migrate_to_v{version}")(connection)
        connection.execute("PRAGMA user_version=9")
        connection.execute("INSERT INTO imports(id,source_filename,sha256,imported_at,start_date,end_date) VALUES (1,'fixture.xlsx','old',?,'2026-10-27','2026-10-27')", (timestamp,))
        for number, status in ((1, "sent"), (2, "pending")):
            connection.execute("INSERT INTO notification_outbox(id,import_id,destination_id,destination_kind,chat_type,chunk_index,text,status,created_at,next_attempt_at,expires_at) VALUES (?,1,123,'user','private',?,'durable',?,?,?,?)", (number, number, status, timestamp, timestamp, "2026-11-03T08:00:00+00:00"))
        connection.execute("INSERT INTO delivery_attempts(notification_id,started_at,outcome,telegram_message_id) VALUES (1,?,'accepted',99)", (timestamp,))
    copy_database(live, source)
    with closing(sqlite3.connect(live)) as connection, connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("INSERT INTO user_profiles VALUES (123,'student','ИС-22',0,?)", (timestamp,))
        connection.execute("UPDATE notification_outbox SET status='sent' WHERE id=2")
        connection.execute("INSERT INTO delivery_attempts(notification_id,started_at,outcome,telegram_message_id) VALUES (2,?,'accepted',100)", (timestamp,))
        connection.execute("INSERT INTO notification_outbox(id,import_id,destination_id,destination_kind,chat_type,chunk_index,text,status,created_at,next_attempt_at,expires_at) VALUES (3,1,123,'user','private',3,'WAL pending','pending',?,?,?)", (timestamp, timestamp, "2026-11-03T08:00:00+00:00"))
        connection.commit()
        for suffix in ("", "-wal", "-shm"):
            shutil.copyfile(str(live) + suffix, str(current) + suffix)
    # Prove the test depends on WAL rather than an accidental checkpoint.
    with closing(sqlite3.connect(current.as_uri() + "?immutable=1", uri=True)) as main_only:
        assert main_only.execute("SELECT * FROM user_profiles").fetchall() == []
        assert main_only.execute("SELECT status FROM notification_outbox WHERE id=2").fetchone() == ("pending",)
        assert main_only.execute("SELECT count(*) FROM delivery_attempts").fetchone() == (1,)
    original = fingerprint(current)
    return SimpleNamespace(
        source=source, current=current, preserved=preserved, original=original,
        snapshot=fingerprint(source),
        sidecars={suffix: Path(str(current) + suffix).read_bytes() for suffix in ("-wal", "-shm")},
    )


def assert_original_delivery_data(path, expected):
    assert fingerprint(path) == expected
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT user_id,target FROM user_profiles").fetchall() == [(123, "ИС-22")]
        assert connection.execute("SELECT id,status FROM notification_outbox ORDER BY id").fetchall() == [(1, "sent"), (2, "sent"), (3, "pending")]
        assert connection.execute("SELECT notification_id,outcome,telegram_message_id FROM delivery_attempts ORDER BY id").fetchall() == [(1, "accepted", 99), (2, "accepted", 100)]


@pytest.mark.parametrize("failure", ["wal", "shm", "replace", "fingerprint", "mismatch", "wal_after", "shm_after", "replace_after"])
def test_restore_wal_failure_restores_committed_delivery_state(stopped_wal_database, monkeypatch, failure):
    db = stopped_wal_database
    rename, replace, check = Path.rename, backup_tools.os.replace, fingerprint
    error = OSError(f"injected {failure}")
    switched = False
    injected = False

    def assert_prepared():
        assert check(db.preserved) == db.original
        candidate = db.current.with_name(db.current.name + ".restore-candidate")
        assert check(candidate) == db.snapshot
        recovery, = db.current.parent.glob(".restore-original-*")
        assert check(recovery) == db.original

    def rename_sidecar(path, target):
        assert_prepared()
        if path == Path(str(db.current) + "-" + failure):
            raise error
        result = rename(path, target)
        if failure.endswith("_after") and path == Path(str(db.current) + "-" + failure.removesuffix("_after")):
            raise error
        return result

    def publish(source, target):
        nonlocal switched
        if Path(source).name.endswith(".restore-candidate"):
            # Both verified originals and the snapshot are ready before the
            # first destructive action, including an independent rollback file.
            assert_prepared()
            if failure == "replace":
                raise error
            switched = True
        result = replace(source, target)
        if failure == "replace_after" and Path(source).name.endswith(".restore-candidate"):
            raise error
        return result

    def verify(path):
        nonlocal injected
        if path == db.current and switched and not injected and failure in {"fingerprint", "mismatch"}:
            injected = True
            if failure == "fingerprint":
                raise error
            return {**check(path), "schema": -1}
        return check(path)

    monkeypatch.setattr(Path, "rename", rename_sidecar)
    monkeypatch.setattr(backup_tools.os, "replace", publish)
    monkeypatch.setattr(backup_tools, "fingerprint", verify)
    with pytest.raises(ValueError if failure == "mismatch" else OSError) as caught:
        restore_stopped_database(db.source, db.current, db.preserved)
    if failure != "mismatch":
        assert caught.value is error
    for suffix in ("-wal", "-shm"):
        diagnostic = Path(str(db.preserved) + ".original" + suffix)
        assert diagnostic.exists()
        if suffix == "-wal":
            assert diagnostic.read_bytes() == db.sidecars[suffix]
    assert_original_delivery_data(db.current, db.original)
    assert_original_delivery_data(db.preserved, db.original)
    assert not list(db.current.parent.glob("*.restore-candidate*"))
    assert not list(db.current.parent.glob(".restore-original-*"))
    # The published recovery file must not share an inode with preserved.
    with closing(sqlite3.connect(db.current)) as connection, connection:
        connection.execute("UPDATE user_profiles SET target='changed'")
    assert fingerprint(db.preserved) == db.original


def test_successful_wal_restore_publishes_snapshot_without_old_sidecars(stopped_wal_database):
    db = stopped_wal_database
    assert restore_stopped_database(db.source, db.current, db.preserved) == db.snapshot
    assert fingerprint(db.current) == db.snapshot
    assert_original_delivery_data(db.preserved, db.original)
    for suffix in ("-wal", "-shm"):
        assert not Path(str(db.current) + suffix).exists()
        assert Path(str(db.preserved) + ".original" + suffix).exists()
    assert db.current.read_bytes()[18:20] == b"\x01\x01"
    assert not list(db.current.parent.glob(".restore-*"))


@pytest.mark.parametrize("failure", ["wal", "shm", "replace"])
def test_failed_compensation_keeps_cause_and_all_recovery_artifacts(stopped_wal_database, monkeypatch, failure):
    db = stopped_wal_database
    after_switch = failure == "replace"
    replace, copy, check = backup_tools.os.replace, shutil.copyfile, fingerprint
    error, secondary = OSError("initial restore failure"), PermissionError("compensation denied")
    switched = False
    restored_shm = False

    def publish(source, target):
        nonlocal switched
        if Path(source).name.endswith(".restore-candidate"):
            if not after_switch:
                raise error
            switched = True
        elif Path(source).name.startswith(".restore-original-"):
            raise secondary
        return replace(source, target)

    def copy_sidecar(source, target, *args, **kwargs):
        nonlocal restored_shm
        if str(source).endswith(".original-" + failure):
            raise secondary
        if str(source).endswith(".original-shm"):
            restored_shm = True
        return copy(source, target, *args, **kwargs)

    def verify(path):
        if switched and path == db.current:
            raise error
        return check(path)

    monkeypatch.setattr(backup_tools.os, "replace", publish)
    monkeypatch.setattr(backup_tools.shutil, "copyfile", copy_sidecar)
    monkeypatch.setattr(backup_tools, "fingerprint", verify)
    with pytest.raises(OSError) as caught:
        restore_stopped_database(db.source, db.current, db.preserved)
    assert caught.value is error
    assert any("compensation denied" in note for note in error.__notes__)
    assert_original_delivery_data(db.preserved, db.original)
    recovery, = db.current.parent.glob(".restore-original-*")
    assert_original_delivery_data(recovery, db.original)
    assert any(str(recovery) in note for note in error.__notes__)
    for suffix in ("-wal", "-shm"):
        assert Path(str(db.preserved) + ".original" + suffix).exists()
    assert not list(db.current.parent.glob("*.restore-candidate*"))
    if after_switch:
        assert check(db.current) == db.snapshot
    elif failure == "wal":
        assert restored_shm  # A failed WAL compensation must not skip SHM.
    else:
        assert_original_delivery_data(db.current, db.original)  # SQLite rebuilds missing SHM.


@pytest.mark.parametrize("stage", ["preserved", "candidate", "recovery", "recovery_fingerprint"])
def test_preparation_failure_never_changes_working_wal_set(stopped_wal_database, monkeypatch, stage):
    db = stopped_wal_database
    copy, copy_file, check = copy_database, shutil.copyfile, fingerprint
    error = OSError("preparation failed")
    original_main = db.current.read_bytes()

    def prepare(source, target):
        result = copy(source, target)
        if stage == "preserved" and target == db.preserved:
            return {**result, "schema": -1}
        if stage == "candidate" and str(target).endswith(".restore-candidate"):
            raise error
        return result

    def prepare_recovery(source, target, *args, **kwargs):
        if stage == "recovery" and Path(target).name.startswith(".restore-original-"):
            raise error
        return copy_file(source, target, *args, **kwargs)

    def verify(path):
        result = check(path)
        if stage == "recovery_fingerprint" and path.name.startswith(".restore-original-"):
            return {**result, "schema": -1}
        return result

    monkeypatch.setattr(backup_tools, "copy_database", prepare)
    monkeypatch.setattr(backup_tools.shutil, "copyfile", prepare_recovery)
    monkeypatch.setattr(backup_tools, "fingerprint", verify)
    monkeypatch.setattr(Path, "rename", lambda *args: pytest.fail("Working set changed during preparation"))
    monkeypatch.setattr(backup_tools.os, "replace", lambda *args: pytest.fail("Published unprepared database"))
    with pytest.raises((OSError, ValueError)):
        restore_stopped_database(db.source, db.current, db.preserved)
    assert db.current.read_bytes() == original_main
    assert Path(str(db.current) + "-wal").read_bytes() == db.sidecars["-wal"]
    assert_original_delivery_data(db.current, db.original)
    assert_original_delivery_data(db.preserved, db.original)
    assert not list(db.current.parent.glob("*.restore-candidate*"))
    assert not list(db.current.parent.glob(".restore-original-*"))


@pytest.mark.parametrize("suffix", [".original-wal", ".original-shm"])
def test_restore_refuses_existing_sidecar_diagnostics(stopped_wal_database, suffix):
    db = stopped_wal_database
    diagnostic = Path(str(db.preserved) + suffix)
    diagnostic.write_bytes(b"previous recovery artifact")
    with pytest.raises(ValueError, match="already exists"):
        restore_stopped_database(db.source, db.current, db.preserved)
    assert diagnostic.read_bytes() == b"previous recovery artifact"
    assert_original_delivery_data(db.current, db.original)


def test_restore_rejects_preserved_candidate_alias(tmp_path):
    source, current = tmp_path / "snapshot.db", tmp_path / "working.db"
    make_database(source, "snapshot")
    make_database(current)
    preserved = current.with_name(current.name + ".restore-candidate")
    before = fingerprint(current)
    with pytest.raises(ValueError, match="paths must differ"):
        restore_stopped_database(source, current, preserved)
    assert fingerprint(current) == before
    assert not preserved.exists()


def test_failed_final_verification_restores_corrupt_original_bytes(tmp_path, monkeypatch):
    source, current, preserved = (tmp_path / name for name in ("snapshot.db", "working.db", "saved.db"))
    make_database(source)
    original = b"corrupt original database"
    current.write_bytes(original)
    check = fingerprint
    error = ValueError("final validation failed")

    def verify(path):
        if path == current and path.read_bytes() != original:
            raise error
        return check(path)

    monkeypatch.setattr(backup_tools, "fingerprint", verify)
    with pytest.raises(ValueError) as caught:
        restore_stopped_database(source, current, preserved)
    assert caught.value is error
    assert current.read_bytes() == preserved.read_bytes() == original
    assert not list(tmp_path.glob(".restore-*"))


def test_failed_final_verification_restores_missing_destination(tmp_path, monkeypatch):
    source, current, preserved = (tmp_path / name for name in ("snapshot.db", "working.db", "saved.db"))
    make_database(source)
    check = fingerprint
    error = ValueError("final validation failed")

    def verify(path):
        if path == current:
            raise error
        return check(path)

    monkeypatch.setattr(backup_tools, "fingerprint", verify)
    with pytest.raises(ValueError) as caught:
        restore_stopped_database(source, current, preserved)
    assert caught.value is error
    assert not current.exists() and not preserved.exists()
    assert not list(tmp_path.glob("*.restore-candidate*"))


def test_cleanup_failure_keeps_original_error_and_recovery_copies(stopped_wal_database, monkeypatch):
    db = stopped_wal_database
    unlink = Path.unlink
    error = OSError("switch failed")

    def fail_publish(*args):
        raise error

    def cleanup(path, *args, **kwargs):
        if path.name.endswith(".restore-candidate"):
            raise PermissionError("candidate cleanup denied")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(backup_tools.os, "replace", fail_publish)
    monkeypatch.setattr(Path, "unlink", cleanup)
    with pytest.raises(OSError) as caught:
        restore_stopped_database(db.source, db.current, db.preserved)
    assert caught.value is error
    assert any("candidate cleanup denied" in note for note in error.__notes__)
    assert_original_delivery_data(db.current, db.original)
    assert_original_delivery_data(db.preserved, db.original)
    candidate = db.current.with_name(db.current.name + ".restore-candidate")
    assert fingerprint(candidate) == db.snapshot
    assert not list(db.current.parent.glob(".restore-original-*"))


def test_recovery_verification_failure_retains_original_error(stopped_wal_database, monkeypatch):
    db = stopped_wal_database
    check = fingerprint
    error = OSError("switch failed")

    def fail_publish(*args):
        raise error

    checks = 0

    def verify(path):
        nonlocal checks
        if path == db.current:
            checks += 1
            if checks > 1:
                raise ValueError("rollback verification failed")
        return check(path)

    monkeypatch.setattr(backup_tools.os, "replace", fail_publish)
    monkeypatch.setattr(backup_tools, "fingerprint", verify)
    with pytest.raises(OSError) as caught:
        restore_stopped_database(db.source, db.current, db.preserved)
    assert caught.value is error
    assert any("rollback verification failed" in note for note in error.__notes__)
    assert_original_delivery_data(db.current, db.original)
    assert_original_delivery_data(db.preserved, db.original)
    recovery, = db.current.parent.glob(".restore-original-*")
    assert_original_delivery_data(recovery, db.original)


@pytest.mark.parametrize("failure", ["recovery_cleanup", "rollback_verification"])
def test_failure_after_switch_retains_original_without_stale_recovery_path(stopped_wal_database, monkeypatch, failure):
    db = stopped_wal_database
    replace, unlink, check = backup_tools.os.replace, Path.unlink, fingerprint
    error = OSError("failure after publication")
    switched = False
    validations = 0

    def publish(source, target):
        nonlocal switched
        result = replace(source, target)
        switched = True
        return result

    def verify(path):
        nonlocal validations
        if switched and path == db.current and failure == "rollback_verification":
            validations += 1
            if validations == 1:
                raise error
            raise ValueError("compensation verification failed")
        return check(path)

    def cleanup(path, *args, **kwargs):
        if failure == "recovery_cleanup" and path.name.startswith(".restore-original-"):
            raise error
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(backup_tools.os, "replace", publish)
    monkeypatch.setattr(backup_tools, "fingerprint", verify)
    monkeypatch.setattr(Path, "unlink", cleanup)
    with pytest.raises(OSError) as caught:
        restore_stopped_database(db.source, db.current, db.preserved)
    assert caught.value is error
    if failure == "rollback_verification":
        assert any("compensation verification failed" in note for note in error.__notes__)
    assert not any("Prepared original database recovery file:" in note for note in error.__notes__)
    assert_original_delivery_data(db.current, db.original)
    assert_original_delivery_data(db.preserved, db.original)
    assert not list(db.current.parent.glob("*.restore-candidate*"))
    assert not list(db.current.parent.glob(".restore-original-*"))
