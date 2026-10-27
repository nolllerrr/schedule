import asyncio
from types import SimpleNamespace

import pytest
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import EditMessageText, SendMessage
from test_pin_safety import CHAT_ID, NOW, TODAY, PinBot, _repository, _sync

from schedule_bot import pinning
from schedule_bot.bot.groups import bot_membership_changed
from schedule_bot.notifier import ScheduleNotifier

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("text_schedule_media")]


@pytest.mark.parametrize("api_error", [False, True])
async def test_bulk_pins_isolate_broken_chat_and_skip_disabled(tmp_path, monkeypatch, api_error):
    repository = _repository(tmp_path)
    first, inactive, disabled = CHAT_ID - 10, CHAT_ID - 9, CHAT_ID - 8
    for chat_id, enabled in ((first, True), (inactive, True), (disabled, False)):
        repository.save_chat_profile(chat_id, chat_type="supergroup", chat_title="test",
                                     target="ИС-22", configured_by=1, pin_enabled=enabled)
    repository.deactivate_chat(inactive)
    assert [p["chat_id"] for p in repository.pinned_chats()] == [first, CHAT_ID]
    original = pinning.sync_chat_schedule_pin
    calls = []

    async def sync(bot, repo, *, chat_id, **kwargs):
        calls.append(chat_id)
        if chat_id == first:
            if api_error:
                raise TelegramNetworkError(method=SendMessage(chat_id=first, text="test"), message="offline")
            raise RuntimeError("bad chat")
        return await original(bot, repo, chat_id=chat_id, **kwargs)

    monkeypatch.setattr(pinning, "sync_chat_schedule_pin", sync)
    bot = PinBot()
    await pinning.sync_schedule_pins(bot, repository, now=NOW)
    assert calls == [first, CHAT_ID]
    assert bot.actions == [("send", 101), ("pin", 101), ("unpin", 10)]
    assert repository.get_chat_profile(CHAT_ID)["last_pinned_message_id"] == 101
    assert repository.get_chat_profile(disabled)["last_pinned_message_id"] is None


@pytest.mark.parametrize("pending", [False, True])
async def test_unchanged_edit_records_hash_without_duplicate_send(tmp_path, pending):
    repository = _repository(tmp_path)
    repository.set_chat_last_pin(CHAT_ID, message_id=10, schedule_date=TODAY, schedule_hash="old")
    if pending:
        repository.set_chat_pending_pin(CHAT_ID, message_id=101, schedule_date=TODAY, schedule_hash="pending")
    bot = PinBot()
    bot.edit_error = TelegramBadRequest(method=EditMessageText(chat_id=CHAT_ID, message_id=101 if pending else 10, text="test"), message="message is not modified")
    assert await _sync(bot, repository) is True
    profile = repository.get_chat_profile(CHAT_ID)
    assert profile["last_pinned_message_id"] == (101 if pending else 10)
    assert profile["pending_pin_message_id"] is None
    assert profile["last_pinned_schedule_hash"] != "old"
    assert bot.sent == []
    assert bot.actions == ([("pin", 101), ("unpin", 10)] if pending else [])
    assert await _sync(bot, repository) is True
    assert bot.sent == []


@pytest.mark.parametrize("status", [ChatMemberStatus.LEFT, ChatMemberStatus.KICKED])
async def test_membership_removal_waits_for_inflight_delivery_and_prevents_next(tmp_path, status):
    repository = _repository(tmp_path)
    repository.save_chat_profile(CHAT_ID, chat_type="supergroup", chat_title="test",
                                 target="ИС-22", configured_by=1, notifications=True,
                                 pin_enabled=True)
    from dataclasses import replace

    from test_pin_safety import _lesson, _parsed
    imported = repository.import_schedule(_parsed(replace(_lesson(), subject="New subject")), sha256="new")
    repository.enqueue_notifications([{
        "import_id": imported.import_id, "destination_id": CHAT_ID, "destination_kind": "chat",
        "chat_type": "supergroup", "chunk_index": 1, "text": "second queued chunk",
    }])
    assert len(repository.pending_notifications()) == 1  # The second chunk waits for the first.
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def send(chat_id, text, **kwargs):
        calls.append(chat_id)
        entered.set()
        await release.wait()
        return SimpleNamespace(message_id=55)

    notifier = ScheduleNotifier(SimpleNamespace(send_message=send), repository, (),
                                broadcast_interval=0, private_interval=0, group_interval=0)
    task = asyncio.create_task(notifier.deliver_pending())
    await asyncio.wait_for(entered.wait(), 5)
    event = SimpleNamespace(chat=SimpleNamespace(id=CHAT_ID, type="supergroup"),
                            new_chat_member=SimpleNamespace(status=status))
    removal = asyncio.create_task(bot_membership_changed(event, repository))
    await asyncio.sleep(0)
    assert not removal.done()
    release.set()
    await asyncio.wait_for(task, 5)
    await asyncio.wait_for(removal, 5)
    assert repository.get_chat_profile(CHAT_ID)["active"] is False
    before = calls[:]
    await notifier.deliver_pending()
    assert calls == before == [CHAT_ID]
    assert repository.notification_outbox_counts() == {"failed": 1, "sent": 1}
    with repository._connect() as connection:
        assert connection.execute(
            "SELECT status, failure_kind FROM notification_outbox WHERE import_id = ? AND chunk_index = 1",
            (imported.import_id,),
        ).fetchone()[:] == ("failed", "cancelled")


@pytest.mark.parametrize("chat_type,status", [("private", ChatMemberStatus.LEFT),
                                               ("supergroup", ChatMemberStatus.MEMBER)])
async def test_irrelevant_membership_event_keeps_chat_active(tmp_path, chat_type, status):
    repository = _repository(tmp_path)
    before = repository.get_chat_profile(CHAT_ID)
    event = SimpleNamespace(chat=SimpleNamespace(id=CHAT_ID, type=chat_type),
                            new_chat_member=SimpleNamespace(status=status))
    await bot_membership_changed(event, repository)
    assert repository.get_chat_profile(CHAT_ID) == before
