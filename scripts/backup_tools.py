"""Offline SQLite checks and restore drills. No network or Telegram operations.

The public restore command always creates a NEW database. In-place restoration
is reserved for the release driver, after it has stopped the only bot instance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path


def connect_readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise ValueError("Database file does not exist")
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def fingerprint(path: Path) -> dict:
    """Check integrity/FKs and hash rows without exposing their contents."""
    with closing(connect_readonly(path)) as connection:
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("Database integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("Database foreign key check failed")
        tables = {}
        for (name,) in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ):
            quoted = '"' + name.replace('"', '""') + '"'
            columns = [r[1] for r in connection.execute(f"PRAGMA table_info({quoted})")]
            order = ",".join('"' + c.replace('"', '""') + '"' for c in columns)
            digest, count = hashlib.sha256(), 0
            for row in connection.execute(f"SELECT * FROM {quoted} ORDER BY {order}"):
                digest.update(json.dumps(row, default=repr, ensure_ascii=True).encode())
                digest.update(b"\n")
                count += 1
            tables[name] = {"columns": columns, "count": count, "sha256": digest.hexdigest()}
        return {
            "schema": connection.execute("PRAGMA user_version").fetchone()[0],
            "tables": tables,
        }


def copy_database(source: Path, destination: Path) -> dict:
    """Publish a verified, consistent copy, refusing an existing destination."""
    if destination.exists():
        raise ValueError("Destination must be a new database")
    if source.resolve() == destination.resolve():
        raise ValueError("Source and destination must differ")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".restore-", dir=destination.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with closing(connect_readonly(source)) as original, closing(sqlite3.connect(temporary)) as target:
            original.backup(target)
            # backup() can copy the source WAL header. Standalone snapshots must
            # be readable on a read-only mount without creating new sidecars.
            if target.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                raise ValueError("Could not finalize standalone database snapshot")
        result = fingerprint(temporary)
        # Hard linking publishes without overwriting a destination created by
        # another process between validation and publication.
        os.link(temporary, destination)
        return result
    finally:
        temporary.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(str(temporary) + suffix).unlink(missing_ok=True)


def restore_stopped_database(source: Path, destination: Path, preserved: Path) -> dict:
    """Internal: caller MUST hold the deployment lock and stop all DB users."""
    expected = fingerprint(source)
    candidate = destination.with_name(destination.name + ".restore-candidate")
    sidecars = [
        (Path(str(destination) + suffix), Path(str(preserved) + ".original" + suffix))
        for suffix in ("-wal", "-shm")
    ]
    candidate_sidecars = [Path(str(candidate) + suffix) for suffix in ("-wal", "-shm")]
    paths = [source, destination, preserved, candidate, *candidate_sidecars,
             *(path for pair in sidecars for path in pair)]
    if len({path.resolve() for path in paths}) != len(paths):
        raise ValueError("Restore database and artifact paths must differ")
    # Never overwrite diagnostic artifacts or remove another restore's candidate.
    for path in (preserved, candidate, *(saved for _, saved in sidecars),
                 *candidate_sidecars):
        if path.exists():
            raise ValueError("Restore preservation or candidate file already exists")
    had_destination = destination.exists()
    if not had_destination and any(path.exists() for path, _ in sidecars):
        raise ValueError("Cannot replace unpreserved SQLite sidecars")

    original_fingerprint = None
    if had_destination:
        try:
            original_fingerprint = fingerprint(destination)
        except (sqlite3.Error, ValueError):
            # Preserve a corrupt failed deployment too. Requiring a valid new
            # database here would prevent recovery precisely when it is needed.
            with destination.open("rb") as original, preserved.open("xb") as saved:
                shutil.copyfileobj(original, saved)
        else:
            if copy_database(destination, preserved) != original_fingerprint:
                raise ValueError("Preserved database differs from the original")

    recovery = None
    moved_sidecars = []
    replace_attempted = False
    switched = False
    try:
        actual = copy_database(source, candidate)
        if actual != expected:
            raise ValueError("Restored database differs from the backup")
        if had_destination:
            # Prepare an independent rollback file BEFORE changing the working
            # set. A hard link to preserved would let future writes damage it.
            descriptor, name = tempfile.mkstemp(prefix=".restore-original-", dir=destination.parent)
            recovery = Path(name)
            os.close(descriptor)
            shutil.copyfile(preserved, recovery)
            if original_fingerprint is not None and fingerprint(recovery) != original_fingerprint:
                raise ValueError("Rollback database differs from the original")

        for sidecar, saved in sidecars:
            if sidecar.exists():
                sidecar.rename(saved)
                moved_sidecars.append((sidecar, saved))
        replace_attempted = True
        os.replace(candidate, destination)
        switched = True
        result = fingerprint(destination)
        if result != expected:
            raise ValueError("Published database differs from the backup")
        if recovery is not None:
            recovery.unlink()
        return result
    except BaseException as error:
        rollback_failed = False
        add_note = error.add_note

        # Compensation must keep the initial exception even if cleanup is
        # interrupted or fails unexpectedly; each secondary error is recorded.
        def record_failure(action: str, secondary: BaseException) -> None:
            add_note(f"{action}: {type(secondary).__name__}: {secondary}")

        # Reconcile an operation that completed before raising (or before an
        # interruption reached Python's bookkeeping). The caller excludes all
        # concurrent users, and replacement consumes the prepared candidate.
        if replace_attempted and not candidate.exists():
            switched = True
        if switched:
            # The old WAL must NEVER be attached to the snapshot or to the
            # consolidated preserved copy. Publish the prepared standalone DB.
            try:
                if recovery is not None:
                    os.replace(recovery, destination)
                    recovery = None
                else:
                    destination.unlink()
                if original_fingerprint is not None and fingerprint(destination) != original_fingerprint:
                    raise ValueError("Rolled back database differs from the original")
            except BaseException as secondary:
                rollback_failed = True
                record_failure("Restoring the original database failed", secondary)
        else:
            # Preserve unmoved sidecars and reconcile completed renames without
            # overwriting any diagnostic artifact.
            for sidecar, saved in sidecars:
                if (sidecar, saved) not in moved_sidecars:
                    try:
                        if saved.exists():
                            # rename may have completed before it raised.
                            moved_sidecars.append((sidecar, saved))
                        elif sidecar.exists():
                            with sidecar.open("rb") as original, saved.open("xb") as diagnostic:
                                shutil.copyfileobj(original, diagnostic)
                    except BaseException as secondary:
                        record_failure(f"Preserving {sidecar.name} failed", secondary)
            # The original main file is still in place. Copy back EACH moved
            # sidecar, retaining the diagnostic copies even after compensation.
            for sidecar, saved in moved_sidecars:
                try:
                    shutil.copyfile(saved, sidecar)
                except BaseException as secondary:
                    rollback_failed = True
                    record_failure(f"Restoring {sidecar.name} failed", secondary)
            if moved_sidecars and not rollback_failed and original_fingerprint is not None:
                try:
                    if fingerprint(destination) != original_fingerprint:
                        raise ValueError("Rolled back database differs from the original")
                except BaseException as secondary:
                    rollback_failed = True
                    record_failure("Verifying the original database rollback failed", secondary)

        # Cleanup must not mask the triggering error, nor delete recovery data
        # when compensation itself failed. Attempt independent cleanups too.
        cleanup = [candidate, *candidate_sidecars]
        if recovery is not None and not rollback_failed:
            cleanup.append(recovery)
        for path in cleanup:
            try:
                path.unlink(missing_ok=True)
            except BaseException as secondary:
                record_failure(f"Removing temporary {path.name} failed", secondary)
        error.add_note(f"Original database preservation: {preserved}; sidecars: {moved_sidecars!r}")
        if rollback_failed and recovery is not None:
            error.add_note(f"Prepared original database recovery file: {recovery}")
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify", help="Read-only integrity/FK verification")
    verify.add_argument("database", type=Path)
    restore = commands.add_parser("restore", help="Restore to a NEW isolated database")
    restore.add_argument("source", type=Path)
    restore.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        result = fingerprint(args.database) if args.command == "verify" else copy_database(args.source, args.destination)
        print(json.dumps({"ok": True, **result}))
    except (ValueError, OSError, sqlite3.Error) as error:
        # Exception values may contain user paths or data; return only the type.
        print(json.dumps({"ok": False, "error": type(error).__name__}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
