import os
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from types import SimpleNamespace

import pytest

from schedule_bot.repository import ScheduleRepository


def test_newer_database_version_is_rejected_without_mutation(tmp_path):
    path = tmp_path / "newer.db"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE sentinel (value TEXT)")
        connection.execute("INSERT INTO sentinel VALUES ('preserve')")
        connection.execute(f"PRAGMA user_version={ScheduleRepository.SCHEMA_VERSION + 1}")
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="newer"):
        ScheduleRepository(path).initialize()
    assert path.read_bytes() == before


def test_two_processes_initialize_same_database_without_losing_data(tmp_path):
    path = tmp_path / "concurrent.db"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE sentinel (value TEXT)")
        connection.execute("INSERT INTO sentinel VALUES ('preserve')")
        connection.commit()
        connection.execute("PRAGMA journal_mode=WAL")
    gate = tmp_path / "start"
    code = (
        "import pathlib,sys,time; from schedule_bot.repository import ScheduleRepository; "
        "gate=pathlib.Path(sys.argv[2]); pathlib.Path(sys.argv[3]).touch(); "
        "deadline=time.monotonic()+15; "
        "exec('while not gate.exists():\\n if time.monotonic()>deadline: raise TimeoutError()\\n time.sleep(0.01)'); "
        "r=ScheduleRepository(sys.argv[1]); r.initialize(); print(r.current_schema_version())"
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src")
    processes = [subprocess.Popen([sys.executable, "-c", code, str(path), str(gate),
                                  str(tmp_path / f"ready-{index}")], env=environment,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for index in range(2)]
    try:
        deadline = time.monotonic() + 15
        while not all((tmp_path / f"ready-{index}").exists() for index in range(2)):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        gate.touch()
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            assert process.returncode == 0, stderr
            assert stdout.strip() == str(ScheduleRepository.SCHEMA_VERSION)
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT value FROM sentinel").fetchall() == [("preserve",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA user_version").fetchone()[0] == ScheduleRepository.SCHEMA_VERSION


def test_initialize_handles_reader_during_transition_to_wal(tmp_path):
    path = tmp_path / "reader.db"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE sentinel (value TEXT)")
        connection.execute("INSERT INTO sentinel VALUES ('preserve')")
    with closing(sqlite3.connect(path, check_same_thread=False)) as reader:
        reader.execute("BEGIN")
        assert reader.execute("SELECT value FROM sentinel").fetchall() == [("preserve",)]
        timer = threading.Timer(0.25, reader.rollback)
        timer.start()
        try:
            ScheduleRepository(path).initialize()
        finally:
            timer.join()
        assert reader.execute("SELECT value FROM sentinel").fetchall() == [("preserve",)]


def test_failed_backup_quick_check_never_publishes_file(tmp_path, monkeypatch):
    repository = ScheduleRepository(tmp_path / "source.db")
    repository.initialize()
    destination = tmp_path / "backup.db"
    original_connect = sqlite3.connect

    class FailedCheck(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql == "PRAGMA quick_check":
                return SimpleNamespace(fetchone=lambda: ("corrupt",))
            return super().execute(sql, *args, **kwargs)

    def connect(*args, **kwargs):
        return original_connect(*args, **kwargs, factory=FailedCheck)

    monkeypatch.setattr(sqlite3, "connect", connect)
    with pytest.raises(RuntimeError, match="integrity"):
        repository.backup_to(destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".backup.db.*.tmp"))
    assert repository.current_schema_version() == ScheduleRepository.SCHEMA_VERSION
