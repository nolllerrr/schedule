import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from schedule_bot.repository import ScheduleRepository
from scripts import backup_tools
from scripts import release_tools as release


@pytest.mark.parametrize("version", [7, 9])
@pytest.mark.parametrize("fail_migration", [False, True])
def test_rehearsal_preserves_historical_profiles_outbox_and_attempts(tmp_path, monkeypatch, version, fail_migration):
    snapshot = tmp_path / "historical.db"
    timestamp = "2026-10-27T08:00:00+00:00"
    with closing(sqlite3.connect(snapshot)) as connection, connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        for number in range(1, version + 1):
            getattr(ScheduleRepository, f"_migrate_to_v{number}")(connection)
        connection.execute(f"PRAGMA user_version={version}")
        connection.execute("INSERT INTO imports(id,source_filename,sha256,imported_at,start_date,end_date) VALUES (1,'historical.xlsx','old',?,'2026-10-27','2026-10-27')", (timestamp,))
        connection.execute("INSERT INTO schedule_dates VALUES (1,'2026-10-27')")
        connection.execute("INSERT INTO user_profiles VALUES (123,'student','ИС-22',0,?)", (timestamp,))
        connection.execute("INSERT INTO notification_outbox(id,import_id,destination_id,destination_kind,chat_type,chunk_index,text,status,created_at,next_attempt_at,expires_at) VALUES (1,1,123,'user','private',0,'durable sent','sent',?,?,?)", (timestamp, timestamp, "2026-11-03T08:00:00+00:00"))
        connection.execute("INSERT INTO notification_outbox(id,import_id,destination_id,destination_kind,chat_type,chunk_index,text,status,created_at,next_attempt_at,expires_at) VALUES (2,1,456,'user','private',0,'durable pending','pending',?,?,?)", (timestamp, timestamp, "2026-11-03T08:00:00+00:00"))
        if version >= 9:
            connection.execute("INSERT INTO delivery_attempts(notification_id,started_at,outcome,telegram_message_id) VALUES (1,?,'accepted',99)", (timestamp,))
    original_bytes = snapshot.read_bytes()
    before = backup_tools.fingerprint(snapshot)
    inspected = tmp_path / "inspected.db"
    def fingerprint(path):
        result = backup_tools.fingerprint(path)
        if path != snapshot:
            backup_tools.copy_database(path, inspected)
        return result
    monkeypatch.setattr(release, "fingerprint", fingerprint)
    if fail_migration:
        error = RuntimeError("injected migration failure")
        def fail(self, connection):
            raise error
        monkeypatch.setattr(ScheduleRepository, "_migrate_to_v10", fail)
        with pytest.raises(RuntimeError) as caught:
            release.database_action(SimpleNamespace(command="database-rehearse", snapshot=snapshot))
        assert caught.value is error
        assert not inspected.exists()
    else:
        result = release.database_action(SimpleNamespace(command="database-rehearse", snapshot=snapshot))
        assert result["schema"] == ScheduleRepository.SCHEMA_VERSION
        with closing(sqlite3.connect(inspected)) as connection:
            assert connection.execute("SELECT user_id,role,target,notifications FROM user_profiles").fetchall() == [(123, "student", "ИС-22", 0)]
            assert connection.execute("SELECT id,text,status FROM notification_outbox ORDER BY id").fetchall() == [(1, "durable sent", "sent"), (2, "durable pending", "pending")]
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
            if version >= 9:
                assert connection.execute("SELECT notification_id,outcome,telegram_message_id FROM delivery_attempts").fetchall() == [(1, "accepted", 99)]
    assert snapshot.read_bytes() == original_bytes
    assert backup_tools.fingerprint(snapshot) == before
