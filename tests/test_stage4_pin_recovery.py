from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
)
from aiogram.methods import (
    EditMessageText,
    GetChatMember,
    SendMessage,
    UnpinChatMessage,
)
from test_pin_safety import (
    CHAT_ID,
    NOW,
    TODAY,
    PinBot,
    _lesson,
    _parsed,
    _repository,
    _sync,
)

from schedule_bot.pinning import unpin_schedule_messages
from schedule_bot.repository import ScheduleRepository

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("text_schedule_media")]


async def test_rejected_send_preserves_database_and_retry_after_restart(tmp_path):
    repository = _repository(tmp_path)
    bot = PinBot()
    original_send = bot.send_message
    bot.send_message = AsyncMock(side_effect=TelegramForbiddenError(
        method=SendMessage(chat_id=CHAT_ID, text="schedule"), message="Forbidden"
    ))
    assert await _sync(bot, repository) is False
    state = repository.get_chat_profile(CHAT_ID)
    assert (state["last_pinned_message_id"], state["pending_pin_message_id"]) == (10, None)
    assert state["pin_attempts"] == 1
    assert bot.actions == []
    restarted = ScheduleRepository(repository.database_path)
    assert await _sync(bot, restarted, now=NOW + timedelta(minutes=1)) is False
    bot.send_message.assert_awaited_once()
    bot.send_message = original_send
    assert await _sync(bot, restarted, now=NOW + timedelta(minutes=5)) is True
    assert bot.actions == [("send", 101), ("pin", 101), ("unpin", 10)]
    assert restarted.get_chat_profile(CHAT_ID)["last_pinned_message_id"] == 101


@pytest.mark.parametrize("error_type", [TelegramBadRequest, TelegramNetworkError])
async def test_failed_edit_of_existing_pin_retains_state_until_retry(tmp_path, error_type):
    repository = _repository(tmp_path)
    repository.set_chat_last_pin(CHAT_ID, message_id=10, schedule_date=TODAY, schedule_hash="old")
    bot = PinBot()
    bot.edit_error = error_type(
        method=EditMessageText(chat_id=CHAT_ID, message_id=10, text="schedule"),
        message="temporary failure",
    )
    assert await _sync(bot, repository) is False
    state = repository.get_chat_profile(CHAT_ID)
    assert (state["last_pinned_message_id"], state["last_pinned_schedule_hash"]) == (10, "old")
    assert state["pending_pin_message_id"] is None
    assert bot.actions == []
    restarted = ScheduleRepository(repository.database_path)
    bot.edit_error = None
    assert await _sync(bot, restarted, now=NOW + timedelta(minutes=5)) is True
    assert len(bot.edited) == 2
    assert bot.actions == []
    assert restarted.get_chat_profile(CHAT_ID)["last_pinned_schedule_hash"] != "old"


async def test_deleted_old_pin_is_replaced_without_touching_other_messages(tmp_path):
    repository = _repository(tmp_path)
    repository.set_chat_last_pin(CHAT_ID, message_id=10, schedule_date=TODAY, schedule_hash="old")
    bot = PinBot()
    bot.edit_error = TelegramBadRequest(
        method=EditMessageText(chat_id=CHAT_ID, message_id=10, text="schedule"),
        message="message to edit not found",
    )
    bot.unpin_error = TelegramBadRequest(
        method=UnpinChatMessage(chat_id=CHAT_ID, message_id=10), message="message to unpin not found"
    )
    assert await _sync(bot, repository) is True
    assert bot.actions == [("send", 101), ("pin", 101), ("unpin", 10)]
    restarted = ScheduleRepository(repository.database_path)
    assert restarted.get_chat_profile(CHAT_ID)["last_pinned_message_id"] == 101
    assert await _sync(bot, restarted) is True
    assert len(bot.sent) == 1


async def test_failed_pending_edit_retains_candidate_and_retries_after_restart(tmp_path):
    repository = _repository(tmp_path)
    bot = PinBot()
    bot.pin_error = TelegramForbiddenError(method=UnpinChatMessage(chat_id=CHAT_ID), message="Forbidden")
    assert await _sync(bot, repository) is False
    repository.import_schedule(_parsed(replace(_lesson(), subject="Физика")), sha256="changed")
    bot.pin_error = None
    bot.edit_error = TelegramNetworkError(
        method=EditMessageText(chat_id=CHAT_ID, message_id=101, text="schedule"), message="timeout"
    )
    assert await _sync(bot, repository, now=NOW + timedelta(minutes=5)) is False
    state = repository.get_chat_profile(CHAT_ID)
    assert (state["last_pinned_message_id"], state["pending_pin_message_id"]) == (10, 101)
    assert state["pending_pin_schedule_hash"] is not None
    assert bot.actions == [("send", 101), ("pin", 101)]
    bot.edit_error = None
    restarted = ScheduleRepository(repository.database_path)
    assert await _sync(bot, restarted, now=NOW + timedelta(minutes=15)) is True
    assert bot.actions == [("send", 101), ("pin", 101), ("pin", 101), ("unpin", 10)]
    assert restarted.get_chat_profile(CHAT_ID)["pending_pin_message_id"] is None


async def test_unpin_failure_keeps_tracking_until_successful_retry(tmp_path):
    repository = _repository(tmp_path)
    repository.set_chat_pending_pin(CHAT_ID, message_id=101, schedule_date=TODAY, schedule_hash="pending")
    bot = PinBot()
    bot.unpin_error = TelegramForbiddenError(
        method=UnpinChatMessage(chat_id=CHAT_ID, message_id=10), message="Forbidden"
    )
    assert await unpin_schedule_messages(bot, repository, chat_id=CHAT_ID) is False
    state = repository.get_chat_profile(CHAT_ID)
    assert (state["last_pinned_message_id"], state["pending_pin_message_id"]) == (10, 101)
    assert bot.actions == [("unpin", 10), ("unpin", 101)]
    bot.unpin_error = None
    restarted = ScheduleRepository(repository.database_path)
    assert await unpin_schedule_messages(bot, restarted, chat_id=CHAT_ID) is True
    state = restarted.get_chat_profile(CHAT_ID)
    assert (state["last_pinned_message_id"], state["pending_pin_message_id"]) == (None, None)

async def test_uncertain_send_defers_without_assuming_telegram_rejected_it(tmp_path):
    repository = _repository(tmp_path)
    bot = PinBot()
    bot.send_message = AsyncMock(side_effect=TelegramNetworkError(
        method=SendMessage(chat_id=CHAT_ID, text="schedule"), message="connection lost"
    ))
    assert await _sync(bot, repository) is False
    state = repository.get_chat_profile(CHAT_ID)
    assert (state["last_pinned_message_id"], state["pending_pin_message_id"]) == (10, None)
    assert state["pin_attempts"] == 1
    assert await _sync(bot, ScheduleRepository(repository.database_path), now=NOW + timedelta(minutes=1)) is False
    bot.send_message.assert_awaited_once()
    assert bot.actions == []


async def test_permission_lookup_error_defers_without_sending(tmp_path):
    repository = _repository(tmp_path)
    bot = PinBot()
    original_lookup = bot.get_chat_member
    bot.get_chat_member = AsyncMock(side_effect=TelegramNetworkError(
        method=GetChatMember(chat_id=CHAT_ID, user_id=bot.id), message="connection lost"
    ))
    assert await _sync(bot, repository) is False
    state = repository.get_chat_profile(CHAT_ID)
    assert (state["last_pinned_message_id"], state["pending_pin_message_id"]) == (10, None)
    assert state["pin_attempts"] == 1
    assert bot.actions == []
    bot.get_chat_member = original_lookup
    assert await _sync(bot, ScheduleRepository(repository.database_path), now=NOW + timedelta(minutes=5)) is True
    assert bot.actions == [("send", 101), ("pin", 101), ("unpin", 10)]
