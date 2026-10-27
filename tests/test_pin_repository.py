from datetime import date, datetime, timedelta, timezone
import sqlite3
from contextlib import closing

from schedule_bot.repository import ScheduleRepository
from schedule_bot.maintenance import backup_before_migration


def chat(repository: ScheduleRepository) -> None:
    repository.save_chat_profile(
        -100, chat_type="supergroup", chat_title="ИС-22",
        target="ИС-22", configured_by=1, pin_enabled=True,
    )


def test_pending_pin_and_backoff_survive_restart(tmp_path) -> None:
    path = tmp_path / 'schedule.db'
    repository = ScheduleRepository(path)
    chat(repository)
    day = date(2026, 10, 14)
    repository.set_chat_last_pin(-100, message_id=1, schedule_date=day, schedule_hash='old')
    repository.set_chat_pending_pin(-100, message_id=2, schedule_date=day, schedule_hash='new')
    now = datetime(2026, 10, 13, 10, tzinfo=timezone.utc)
    repository.defer_chat_pin(-100, now=now)
    profile = ScheduleRepository(path).get_chat_profile(-100)
    assert profile['last_pinned_message_id'] == 1
    assert profile['pending_pin_message_id'] == 2
    assert profile['pending_pin_schedule_date'] == day
    assert profile['pending_pin_schedule_hash'] == 'new'
    assert profile['pin_retry_after'] == now + timedelta(minutes=5)
    assert profile['pin_attempts'] == 1
    repository.defer_chat_pin(-100, now=now)
    assert repository.get_chat_profile(-100)['pin_retry_after'] == now + timedelta(minutes=10)
    for _ in range(20):
        repository.defer_chat_pin(-100, now=now)
    assert repository.get_chat_profile(-100)['pin_retry_after'] == now + timedelta(hours=1)


def test_success_clears_pending_but_reconfiguration_keeps_old_pin(tmp_path) -> None:
    repository = ScheduleRepository(tmp_path / 'schedule.db')
    chat(repository)
    day = date(2026, 10, 14)
    repository.set_chat_pending_pin(-100, message_id=2, schedule_date=day, schedule_hash='new')
    repository.set_chat_last_pin(-100, message_id=2, schedule_date=day, schedule_hash='new')
    profile = repository.get_chat_profile(-100)
    assert profile['pending_pin_message_id'] is None
    assert profile['pin_attempts'] == 0
    assert profile['last_pinned_schedule_date'] == day
    repository.save_chat_profile(-100, chat_type='supergroup', chat_title='ПД-12',
                                 target='ПД-12', configured_by=1)
    profile = repository.get_chat_profile(-100)
    assert profile['last_pinned_message_id'] == 2
    assert profile['last_pinned_schedule_date'] is None
    assert profile['last_pinned_schedule_hash'] is None
    assert profile['pin_enabled'] is True
    repository.clear_chat_last_pin(-100)
    assert repository.get_chat_profile(-100)['last_pinned_message_id'] is None


def test_v5_to_v6_backup_preserves_confirmed_pin(tmp_path) -> None:
    path = tmp_path / 'schedule.db'
    with closing(sqlite3.connect(path)) as connection, connection:
        for migration in (
            ScheduleRepository._migrate_to_v1, ScheduleRepository._migrate_to_v2,
            ScheduleRepository._migrate_to_v3, ScheduleRepository._migrate_to_v4,
            ScheduleRepository._migrate_to_v5,
        ):
            migration(connection)
        connection.execute(
            "INSERT INTO chat_profiles(chat_id,chat_type,target,configured_by,created_at,updated_at,"
            "pin_enabled,last_pinned_message_id,last_pinned_schedule_date,last_pinned_schedule_hash) "
            "VALUES (-100,'supergroup','ИС-22',1,'now','now',1,17,'2026-10-14','hash')"
        )
        connection.execute('PRAGMA user_version = 5')
    repository = ScheduleRepository(path)
    backup = backup_before_migration(repository, tmp_path / 'backups')
    assert backup is not None
    repository.initialize()
    profile = repository.get_chat_profile(-100)
    assert profile['last_pinned_message_id'] == 17
    assert profile['last_pinned_schedule_hash'] == 'hash'
    assert profile['pending_pin_message_id'] is None
    assert profile['pin_retry_after'] is None
    assert profile['pin_attempts'] == 0
    with closing(sqlite3.connect(backup)) as connection, connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 5
        assert connection.execute('SELECT last_pinned_message_id FROM chat_profiles').fetchone()[0] == 17
