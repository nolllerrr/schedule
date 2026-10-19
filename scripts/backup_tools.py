"""Offline SQLite checks and restore drills. No network or Telegram operations.

The public restore command always creates a NEW database. In-place restoration
is reserved for the release driver, after it has stopped the only bot instance.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile


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
    if destination.exists():
        if preserved.exists():
            raise ValueError("Failed deployment preservation file already exists")
        try:
            copy_database(destination, preserved)
        except (sqlite3.Error, ValueError):
            # Preserve a corrupt failed deployment too. Requiring a valid new
            # database here would prevent recovery precisely when it is needed.
            with destination.open("rb") as original, preserved.open("xb") as saved:
                shutil.copyfileobj(original, saved)
    candidate = destination.with_name(destination.name + ".restore-candidate")
    actual = copy_database(source, candidate)
    try:
        if actual != expected:
            raise ValueError("Restored database differs from the backup")
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(destination) + suffix)
            if sidecar.exists():
                if not preserved.exists():
                    raise ValueError("Cannot replace unpreserved SQLite sidecars")
                sidecar.rename(preserved.with_name(preserved.name + ".original" + suffix))
        os.replace(candidate, destination)
        return fingerprint(destination)
    finally:
        candidate.unlink(missing_ok=True)


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
