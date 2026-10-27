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
    PinChatMessage,
    SendMessage,
    UnpinChatMessage,
)
from test_pin_safety import CHAT_ID, NOW, TODAY, PinBot, _repository, _sync

from schedule_bot.repository import ScheduleRepository

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("text_schedule_media")]


@pytest.mark.parametrize("error_type", [TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError])
@pytest.mark.parametrize("stage", ["permission", "publish", "confirmed_edit", "pending_edit", "pin", "unpin", "retire"])
async def test_pin_error_matrix_preserves_state_and_recovers_after_restart(tmp_path, error_type, stage):
    repo = _repository(tmp_path)
    bot = PinBot()
    if stage == "confirmed_edit":
        repo.set_chat_last_pin(CHAT_ID, message_id=10, schedule_date=TODAY, schedule_hash="old")
    if stage in {"pending_edit", "retire"}:
        repo.set_chat_pending_pin(CHAT_ID, message_id=101, schedule_date=TODAY, schedule_hash="candidate")
        bot.sent.append((CHAT_ID, "existing candidate"))
    methods = {
        "permission": GetChatMember(chat_id=CHAT_ID, user_id=bot.id),
        "publish": SendMessage(chat_id=CHAT_ID, text="test"),
        "confirmed_edit": EditMessageText(chat_id=CHAT_ID, message_id=10, text="test"),
        "pending_edit": EditMessageText(chat_id=CHAT_ID, message_id=101, text="test"),
        "pin": PinChatMessage(chat_id=CHAT_ID, message_id=101),
        "unpin": UnpinChatMessage(chat_id=CHAT_ID, message_id=10),
        "retire": UnpinChatMessage(chat_id=CHAT_ID, message_id=101),
    }
    error = error_type(method=methods[stage], message="synthetic transient error")
    original_lookup, original_send = bot.get_chat_member, bot.send_message
    if stage == "permission":
        bot.get_chat_member = AsyncMock(side_effect=error)
    elif stage == "publish":
        bot.send_message = AsyncMock(side_effect=error)
    elif stage in {"confirmed_edit", "pending_edit"}:
        bot.edit_error = error
    elif stage == "pin":
        bot.pin_error = error
    else:
        bot.unpin_error = error
        if stage == "retire":
            bot.edit_error = TelegramBadRequest(method=methods["pending_edit"], message="message can't be edited")
    assert await _sync(bot, repo) is False
    state = repo.get_chat_profile(CHAT_ID)
    assert state["last_pinned_message_id"] == 10
    assert state["pin_attempts"] == 1
    assert state["pin_retry_after"] is not None
    assert state["pending_pin_message_id"] == (101 if stage in {"pending_edit", "pin", "unpin", "retire"} else None)
    actions = bot.actions[:]
    restarted = ScheduleRepository(repo.database_path)
    assert await _sync(bot, restarted, now=NOW + timedelta(minutes=1)) is False
    assert bot.actions == actions
    bot.get_chat_member, bot.send_message = original_lookup, original_send
    bot.pin_error = bot.unpin_error = None
    if stage != "retire":
        bot.edit_error = None
    assert await _sync(bot, restarted, now=NOW + timedelta(minutes=5)) is True
    final = restarted.get_chat_profile(CHAT_ID)
    assert final["pending_pin_message_id"] is None
    assert final["pin_attempts"] == 0 and final["pin_retry_after"] is None
    assert final["last_pinned_schedule_hash"] not in {"old", "candidate", "old-pin"}
    assert final["last_pinned_message_id"] == (10 if stage == "confirmed_edit" else 102 if stage == "retire" else 101)
    after = (bot.actions[:], bot.sent[:], bot.edited[:])
    assert await _sync(bot, restarted, now=NOW + timedelta(minutes=5)) is True
    assert (bot.actions, bot.sent, bot.edited) == after
