"""Offline diagnostic: run directly; exit 1 when concurrent first WAL setup fails."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path

CHILD = '''
import pathlib, sys, time, sqlite3
from schedule_bot.repository import ScheduleRepository
path, index = sys.argv[1:]
original_connect = sqlite3.connect
trace_errors = []
def connect(*args, **kwargs):
    connection = original_connect(*args, **kwargs)
    def trace(sql):
        if sql == "PRAGMA journal_mode = WAL":
            pathlib.Path(path + ".ready-" + index).touch()
            deadline = time.monotonic() + 15
            while not all(pathlib.Path(path + ".ready-" + str(i)).exists() for i in range(2)):
                if time.monotonic() > deadline:
                    trace_errors.append("rendezvous timeout")
                    return
                time.sleep(0.001)
    connection.set_trace_callback(trace)
    return connection
sqlite3.connect = connect
repository = ScheduleRepository(path)
repository.initialize()
assert not trace_errors, trace_errors
print(repository.current_schema_version())
'''


def main(rounds=10):
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    results = []
    with tempfile.TemporaryDirectory(prefix="schedule-wal-probe-") as directory:
        for index in range(rounds):
            path = Path(directory) / f"round-{index}.db"
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("CREATE TABLE sentinel(value TEXT)")
                connection.execute("INSERT INTO sentinel VALUES ('preserve')")
            processes = [subprocess.Popen([sys.executable, "-c", CHILD, str(path), str(worker)],
                env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for worker in range(2)]
            try:
                outcomes = []
                for process in processes:
                    stdout, stderr = process.communicate(timeout=30)
                    outcomes.append({"exit": process.returncode, "stdout": stdout.strip(), "stderr": stderr.strip()})
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.communicate()
            with closing(sqlite3.connect(path)) as connection:
                assert connection.execute("SELECT value FROM sentinel").fetchall() == [("preserve",)]
                assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
            results.append(outcomes)
    failures = sum(any(outcome["exit"] for outcome in result) for result in results)
    print(json.dumps({"rounds": rounds, "failed_rounds": failures, "outcomes": results}, ensure_ascii=True))
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
