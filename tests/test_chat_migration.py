from __future__ import annotations

import asyncio
import sqlite3
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramMigrateToChat, TelegramRetryAfter
from aiogram.enums import ChatMemberStatus
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message

from schedule_bot.bot.groups import group_migrated, router
from schedule_bot.delivery_audit import begin_attempt, record_attempt
from schedule_bot.domain import Lesson, ParsedSchedule
from schedule_bot.notifier import ScheduleNotifier, _destination_lock, migrate_group_chat
from schedule_bot.pinning import sync_chat_schedule_pin
from schedule_bot.repository import ScheduleRepository


OLD = -123
NEW = -100123
NOW = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)
DAY = date(2026, 10, 26)


def save_chat(repository, chat_id=OLD, **kwargs):
    repository.save_chat_profile(
        chat_id, chat_type="group" if chat_id == OLD else "supergroup",
        chat_title="Учебная группа", target=kwargs.pop("target", "ИС-22"),
        configured_by=42, **kwargs,
    )


@pytest.fixture
def repository(tmp_path):
    repo = ScheduleRepository(tmp_path / "migration.db")
    lesson = Lesson(DAY, "ИС-22", 1, "1 пара", 1, "09:00", "10:30", "Математика")
    repo.import_schedule(ParsedSchedule("26.10.xlsx", DAY, DAY, (DAY,), (lesson,)), sha256="source")
    save_chat(repo, pin_enabled=True)
    return repo


def enqueue(repository, chat_id=OLD, *, chunks=2, card=False):
    repository.enqueue_notifications([
        dict(
            import_id=1, destination_id=chat_id, destination_kind="chat",
            chat_type="group" if chat_id == OLD else "supergroup",
            chunk_index=index, text=f"part {index}",
            card_payload={
                "date": DAY.isoformat(), "role": "student", "target": "ИС-22", "lessons": [],
            } if card else None,
        ) for index in range(chunks)
    ], now=NOW)


def rows(repository, table="notification_outbox"):
    with repository._connect() as connection:
        return [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY id")]


def worker(repository, bot):
    return ScheduleNotifier(
        bot, repository, (), broadcast_interval=0, private_interval=0, group_interval=0,
        clock=lambda: NOW,
    )


def migration_error(new_id=NEW):
    return TelegramMigrateToChat(
        method=SendMessage(chat_id=OLD, text="schedule"), message="Migrated",
        migrate_to_chat_id=new_id,
    )


def test_migration_moves_subscription_outbox_and_resets_old_pin_ids(repository):
    enqueue(repository)
    repository.set_chat_last_pin(OLD, message_id=7, schedule_date=DAY, schedule_hash="old-pin")
    repository.set_chat_pending_pin(OLD, message_id=8, schedule_date=DAY, schedule_hash="pending")
    repository.defer_chat_pin(OLD, now=NOW)
    repository.set_chat_notifications(OLD, False)
    before = repository.get_chat_profile(OLD)
    repository.mark_notification_sent(rows(repository)[0]["id"])

    assert repository.migrate_chat(OLD, NEW, now=NOW)
    assert repository.get_chat_profile(OLD) is None
    profile = repository.get_chat_profile(NEW)
    for field in ("target", "notifications", "pin_enabled", "configured_by", "created_at", "active"):
        assert profile[field] == before[field]
    assert profile["chat_type"] == "supergroup"
    for field in ("last_pinned_message_id", "last_pinned_schedule_hash", "last_pinned_schedule_date",
                  "pending_pin_message_id", "pending_pin_schedule_hash", "pending_pin_schedule_date", "pin_retry_after"):
        assert profile[field] is None
    assert profile["pin_attempts"] == 0
    assert {row["destination_id"] for row in rows(repository)} == {NEW}
    assert {row["chat_type"] for row in rows(repository)} == {"supergroup"}
    assert [row["status"] for row in rows(repository)] == ["sent", "pending"]
    assert not repository.migrate_chat(OLD, NEW, now=NOW)


@pytest.mark.parametrize("sent_at", [OLD, NEW, None])
def test_duplicate_outbox_keeps_sent_state_and_attempt_evidence(repository, sent_at):
    save_chat(repository, NEW, notifications=False)
    enqueue(repository)
    enqueue(repository, NEW)
    for row in rows(repository):
        attempt = begin_attempt(repository, notification_id=row["id"], now=NOW)
        if row["destination_id"] == sent_at:
            repository.mark_notification_sent(row["id"])
            record_attempt(repository, attempt, "sent", telegram_message_id=42, now=NOW)
    repository.migrate_chat(OLD, NEW, now=NOW)

    assert len(rows(repository)) == 2
    assert len(rows(repository, "delivery_attempts")) == 4
    assert {row["destination_id"] for row in rows(repository)} == {NEW}
    assert {row["status"] for row in rows(repository)} == ({"sent"} if sent_at else {"pending"})
    assert repository.get_chat_profile(NEW)["notifications"] is False
    with repository._connect() as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_conflicting_destination_setup_wins_without_sending_old_target(repository):
    enqueue(repository)
    save_chat(repository, NEW, target="ПД-12", pin_enabled=False)
    enqueue(repository, NEW, chunks=1)
    repository.set_chat_last_message(NEW, 900)
    repository.migrate_chat(OLD, NEW, now=NOW)

    profile = repository.get_chat_profile(NEW)
    assert profile["target"] == "ПД-12"
    assert profile["last_pinned_message_id"] == 900
    assert profile["pin_enabled"] is False
    assert [(row["chunk_index"], row["status"], row["target_snapshot"]) for row in rows(repository)] == [
        (1, "failed", "ИС-22"), (0, "pending", "ПД-12"),
    ]


def test_migration_releases_only_migration_backoff_not_terminal_messages(repository):
    enqueue(repository, chunks=1)
    delivery_id = rows(repository)[0]["id"]
    repository.mark_notification_failed(delivery_id, "TelegramMigrateToChat: migrated", now=NOW)
    repository.migrate_chat(OLD, NEW, now=NOW)
    assert repository.notification_is_sendable(delivery_id, now=NOW, expected_destination_id=NEW)
    assert not repository.notification_is_sendable(delivery_id, now=NOW, expected_destination_id=OLD)


def test_expired_messages_stay_terminal(repository):
    enqueue(repository)
    repository.pending_notifications(now=NOW + timedelta(days=8))
    repository.migrate_chat(OLD, NEW, now=NOW + timedelta(days=8))
    assert {row["failure_kind"] for row in rows(repository)} == {"expired"}


def test_transaction_rolls_back_profile_and_queue_together(repository):
    enqueue(repository)
    with repository._connect() as connection:
        connection.execute("CREATE TRIGGER reject_move BEFORE UPDATE OF destination_id ON notification_outbox "
                           "BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
        connection.commit()
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        repository.migrate_chat(OLD, NEW)
    assert repository.get_chat_profile(OLD) is not None
    assert repository.get_chat_profile(NEW) is None
    assert {row["destination_id"] for row in rows(repository)} == {OLD}


@pytest.mark.parametrize("old_id,new_id", [(123, NEW), (OLD, 123), (OLD, OLD), (True, NEW)])
def test_invalid_migration_cannot_change_profiles(repository, old_id, new_id):
    with pytest.raises(ValueError):
        repository.migrate_chat(old_id, new_id)
    assert repository.get_chat_profile(OLD) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("photo", [False, True])
async def test_delivery_migrates_then_retries_text_or_photo(repository, monkeypatch, photo):
    enqueue(repository, card=photo)
    monkeypatch.setattr("schedule_bot.notifier.schedule_photo", AsyncMock(
        return_value=SimpleNamespace(media=b"photo", caption="schedule"),
    ))
    send = AsyncMock(side_effect=[migration_error(), SimpleNamespace(message_id=1), SimpleNamespace(message_id=2)])
    bot = SimpleNamespace(send_photo=send, send_message=send)
    assert await asyncio.wait_for(worker(repository, bot).deliver_pending(), timeout=5) == 2
    assert [call.args[0] for call in send.call_args_list] == [OLD, NEW, NEW]
    assert repository.notification_outbox_counts() == {"sent": 2}
    attempts = rows(repository, "delivery_attempts")
    assert [row["outcome"] for row in attempts] == ["rejected", "sent", "sent"]
    assert attempts[0]["error_category"] == "TelegramMigrateToChat"
    assert repository.get_chat_profile(OLD) is None


@pytest.mark.asyncio
async def test_retry_after_still_works_after_migration(repository, monkeypatch):
    enqueue(repository, chunks=1)
    send = AsyncMock(side_effect=[migration_error(), TelegramRetryAfter(
        method=SendMessage(chat_id=NEW, text="part"), message="Flood", retry_after=1,
    ), SimpleNamespace(message_id=1)])
    notifier = worker(repository, SimpleNamespace(send_message=send))
    # Keep the real retry bookkeeping while advancing its deterministic clock.
    async def wait(*args):
        next_tick = notifier._clock() + timedelta(seconds=2)
        notifier._clock = lambda: next_tick
    monkeypatch.setattr(notifier, "_wait_send_slot", wait)
    assert await notifier.deliver_pending() == 1
    assert [call.args[0] for call in send.call_args_list] == [OLD, NEW, NEW]


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["to", "from"])
async def test_service_migration_is_idempotent(repository, direction):
    enqueue(repository)
    message = SimpleNamespace(
        chat=SimpleNamespace(id=OLD if direction == "to" else NEW),
        migrate_to_chat_id=NEW if direction == "to" else None,
        migrate_from_chat_id=OLD if direction == "from" else None,
    )
    await group_migrated(message, repository)
    await group_migrated(message, repository)
    assert repository.get_chat_profile(OLD) is None
    assert repository.get_chat_profile(NEW)["target"] == "ИС-22"
    assert len(rows(repository)) == 2


@pytest.mark.asyncio
async def test_stale_candidate_after_service_migration_never_sends_to_old_id(repository):
    enqueue(repository, chunks=1)
    candidate = repository.pending_notifications(now=NOW)[0]
    await migrate_group_chat(repository, OLD, NEW, now=NOW)
    send = AsyncMock(return_value=SimpleNamespace(message_id=1))
    notifier = worker(repository, SimpleNamespace(send_message=send))
    assert await notifier._deliver(candidate)
    send.assert_awaited_once_with(NEW, "part 0")


@pytest.mark.asyncio
async def test_two_senders_and_service_migration_do_not_duplicate_delivery(repository):
    enqueue(repository, chunks=1)
    entered, release = asyncio.Event(), asyncio.Event()
    candidate = repository.pending_notifications(now=NOW)[0]
    calls = []
    async def send(chat_id, text):
        calls.append(chat_id)
        if chat_id == OLD:
            entered.set()
            await release.wait()
            raise migration_error()
        return SimpleNamespace(message_id=1)
    first = worker(repository, SimpleNamespace(send_message=send))
    second = worker(ScheduleRepository(repository.database_path), SimpleNamespace(send_message=send))
    tasks = [asyncio.create_task(first._deliver(candidate))]
    await entered.wait()
    tasks.extend([
        asyncio.create_task(second._deliver(candidate)),
        asyncio.create_task(migrate_group_chat(repository, OLD, NEW, now=NOW)),
    ])
    release.set()
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
    assert calls.count(NEW) == 1
    assert repository.notification_outbox_counts() == {"sent": 1}


@pytest.mark.asyncio
async def test_migration_waits_for_new_destination_send(repository):
    enqueue(repository, chunks=1)
    entered = asyncio.Event()
    async def migrate():
        entered.set()
        return await migrate_group_chat(repository, OLD, NEW, now=NOW)
    async with _destination_lock(repository, "chat", NEW):
        task = asyncio.create_task(migrate())
        await entered.wait()
        await asyncio.sleep(0)
        assert not task.done()
    assert await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_restart_after_atomic_move_resumes_at_new_id(repository):
    enqueue(repository)
    first = repository.pending_notifications(now=NOW)[0]
    attempt = begin_attempt(repository, notification_id=first["id"], now=NOW)
    record_attempt(repository, attempt, "rejected", error_category="TelegramMigrateToChat", now=NOW)
    repository.migrate_chat(OLD, NEW, now=NOW)
    restarted = ScheduleRepository(repository.database_path)
    send = AsyncMock(return_value=SimpleNamespace(message_id=1))
    assert await worker(restarted, SimpleNamespace(send_message=send)).deliver_pending() == 2
    assert [call.args[0] for call in send.call_args_list] == [NEW, NEW]


@pytest.mark.asyncio
async def test_service_migration_during_send_wait_prevents_old_id_request(repository, monkeypatch):
    enqueue(repository, chunks=1)
    candidate = repository.pending_notifications(now=NOW)[0]
    send = AsyncMock(return_value=SimpleNamespace(message_id=1))
    notifier = worker(repository, SimpleNamespace(send_message=send))
    async def wait(*args):
        await migrate_group_chat(repository, OLD, NEW, now=NOW)
    monkeypatch.setattr(notifier, "_wait_send_slot", wait)
    assert not await notifier._deliver(candidate)
    send.assert_not_awaited()
    assert await notifier.deliver_pending() == 1
    assert send.call_args.args[0] == NEW


@pytest.mark.asyncio
async def test_existing_confirmed_delivery_is_not_resent_after_migration_error(repository):
    enqueue(repository, chunks=1)
    save_chat(repository, NEW)
    enqueue(repository, NEW, chunks=1)
    destination_row = rows(repository)[1]
    repository.mark_notification_sent(destination_row["id"])
    send = AsyncMock(side_effect=migration_error())
    assert await worker(repository, SimpleNamespace(send_message=send)).deliver_pending() == 0
    assert send.await_count == 1
    assert repository.notification_outbox_counts() == {"sent": 1}


@pytest.mark.asyncio
async def test_disabled_destination_is_not_resubscribed_by_send_migration(repository):
    enqueue(repository, chunks=1)
    save_chat(repository, NEW, notifications=False)
    send = AsyncMock(side_effect=migration_error())
    assert await worker(repository, SimpleNamespace(send_message=send)).deliver_pending() == 0
    assert send.await_count == 1
    assert repository.get_chat_profile(NEW)["notifications"] is False
    assert repository.notification_outbox_counts() == {"failed": 1}


@pytest.mark.asyncio
async def test_repeated_redirects_are_bounded_and_stay_rejected(repository):
    enqueue(repository, chunks=1)
    send = AsyncMock(side_effect=migration_error())
    assert await worker(repository, SimpleNamespace(send_message=send)).deliver_pending() == 0
    assert send.await_count == 2
    assert {row["outcome"] for row in rows(repository, "delivery_attempts")} == {"rejected"}
    assert repository.notification_outbox_counts() == {"pending": 1}


def test_migration_does_not_touch_private_destinations_or_create_unknown_profiles(repository):
    enqueue(repository, chunks=1)
    repository.save_profile(42, role="student", target="ИС-22")
    repository.enqueue_notifications([dict(
        import_id=1, destination_id=42, destination_kind="user", chat_type="private",
        chunk_index=0, text="private schedule",
    )], now=NOW)
    assert not repository.migrate_chat(-999, -100999, now=NOW)
    assert repository.get_chat_profile(-100999) is None
    repository.migrate_chat(OLD, NEW, now=NOW)
    private = [row for row in rows(repository) if row["destination_kind"] == "user"]
    assert [(row["destination_id"], row["chat_type"], row["status"]) for row in private] == [
        (42, "private", "pending"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["to", "from"])
async def test_registered_filter_accepts_real_migration_service_messages(direction):
    message = Message(
        message_id=1, date=NOW,
        chat=Chat(id=OLD if direction == "to" else NEW, type="group" if direction == "to" else "supergroup"),
        **({"migrate_to_chat_id": NEW} if direction == "to" else {"migrate_from_chat_id": OLD}),
    )
    handler = next(item for item in router.message.handlers if item.callback is group_migrated)
    accepted, _ = await handler.check(message)
    assert accepted


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [False, True])
async def test_new_pin_checks_permissions_without_using_old_message_ids(repository, monkeypatch, allowed):
    repository.set_chat_last_message(OLD, 700)
    repository.migrate_chat(OLD, NEW, now=NOW)
    monkeypatch.setattr("schedule_bot.pinning.schedule_photo", AsyncMock(return_value=None))
    bot = SimpleNamespace(
        id=999, get_chat_member=AsyncMock(return_value=SimpleNamespace(
            status=ChatMemberStatus.ADMINISTRATOR, can_pin_messages=allowed,
        )), send_message=AsyncMock(return_value=SimpleNamespace(message_id=901)),
        pin_chat_message=AsyncMock(), unpin_chat_message=AsyncMock(), edit_message_text=AsyncMock(),
    )
    assert await sync_chat_schedule_pin(bot, repository, chat_id=NEW, target="ИС-22", now=NOW) is allowed
    bot.get_chat_member.assert_awaited_once_with(NEW, 999)
    bot.unpin_chat_message.assert_not_awaited()
    bot.edit_message_text.assert_not_awaited()
    if allowed:
        assert bot.send_message.call_args.args[0] == NEW
        assert repository.get_chat_profile(NEW)["last_pinned_message_id"] == 901
    else:
        bot.send_message.assert_not_awaited()
        bot.pin_chat_message.assert_not_awaited()
