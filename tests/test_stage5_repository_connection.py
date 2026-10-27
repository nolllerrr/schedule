import sqlite3

import pytest

from schedule_bot.repository import ScheduleRepository


def test_connection_is_closed_when_pragma_setup_fails(tmp_path, monkeypatch):
    class FailingConnection(sqlite3.Connection):
        closed = False

        def execute(self, sql, *args, **kwargs):
            if sql.startswith("PRAGMA busy_timeout"):
                raise sqlite3.OperationalError("injected setup failure")
            return super().execute(sql, *args, **kwargs)

        def close(self):
            self.closed = True
            return super().close()

    real_connect = sqlite3.connect
    connections = []

    def connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs, factory=FailingConnection)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    try:
        with (
            pytest.raises(sqlite3.OperationalError, match="injected setup failure"),
            ScheduleRepository(tmp_path / "setup.db")._connect(),
        ):
            pytest.fail("setup unexpectedly succeeded")
        assert len(connections) == 1
        assert connections[0].closed
    finally:
        for connection in connections:
            if not connection.closed:
                connection.close()
