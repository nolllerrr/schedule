from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.enums import ChatMemberStatus
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from test_groups import _database_snapshot, _permission_bot, _permission_callback
from test_pin_safety import _repository

from schedule_bot.bot import app, groups

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("handler", [groups.save_group_setup, groups.group_menu_action])
async def test_group_callback_without_message_has_no_effects(tmp_path, handler):
    repo = _repository(tmp_path)
    before = _database_snapshot(repo)
    callback = _permission_callback("group_setup:123:ИС-22" if handler is groups.save_group_setup else "gnav:123:settings")
    callback.message = None
    bot = _permission_bot(ChatMemberStatus.ADMINISTRATOR)
    analytics = SimpleNamespace(track=AsyncMock())
    await handler(callback, bot, repo, None, analytics)
    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs["show_alert"] is True
    bot.get_chat_member.assert_not_awaited()
    analytics.track.assert_not_awaited()
    assert _database_snapshot(repo) == before


async def test_setup_message_without_sender_cannot_change_chat(tmp_path):
    repo = _repository(tmp_path)
    before = _database_snapshot(repo)
    bot = _permission_bot(ChatMemberStatus.ADMINISTRATOR)
    message = SimpleNamespace(from_user=None, chat=SimpleNamespace(id=-100123), answer=AsyncMock())
    await groups.setup_group(message, bot, repo)
    bot.get_chat_member.assert_not_awaited()
    message.answer.assert_awaited_once()
    assert _database_snapshot(repo) == before


async def test_private_start_without_sender_clears_state_without_analytics():
    storage = MemoryStorage()
    state = FSMContext(storage, StorageKey(bot_id=1, chat_id=1, user_id=1))
    await state.set_state(app.ProfileSetup.choosing_teacher)
    await state.update_data(teacher_matches=["old"])
    message = SimpleNamespace(from_user=None, answer=AsyncMock())
    analytics = SimpleNamespace(track=AsyncMock())
    await app.start(message, state, analytics)
    assert await state.get_state() is None
    assert await state.get_data() == {}
    analytics.track.assert_not_awaited()
    message.answer.assert_awaited_once()
    await storage.close()


@pytest.mark.parametrize("handler", ["student_list", "teacher_prompt", "student_save", "teacher_save"])
@pytest.mark.xfail(strict=True, reason="private profile handlers dereference absent callback.message before acknowledgment")
async def test_inline_profile_callback_without_message_is_acknowledged(tmp_path, handler):
    repo = _repository(tmp_path)
    storage = MemoryStorage()
    state = FSMContext(storage, StorageKey(bot_id=1, chat_id=123, user_id=123))
    await state.set_state(app.ProfileSetup.choosing_teacher)
    await state.update_data(teacher_matches=["Иванов И.И."], teacher_search_id="test")
    before = _database_snapshot(repo)
    analytics = SimpleNamespace(track=AsyncMock())
    callback = SimpleNamespace(message=None, from_user=SimpleNamespace(id=123),
                               data="teacher:test:0" if handler == "teacher_save" else "group:ИС-22",
                               answer=AsyncMock())
    try:
        if handler == "student_list":
            await app.choose_student_group(callback, repo, state)
        elif handler == "teacher_prompt":
            await app.request_teacher(callback, state)
        elif handler == "student_save":
            await app.save_student_group(callback, repo, analytics)
        else:
            await app.save_teacher(callback, repo, state, analytics)
        callback.answer.assert_awaited_once()
        assert _database_snapshot(repo) == before
        analytics.track.assert_not_awaited()
    finally:
        await storage.close()
