from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from schedule_bot.bot import app
from schedule_bot.domain import Lesson, ParsedSchedule
from schedule_bot.repository import ScheduleRepository

USER = 101
OTHER = 202
DAY = date(2026, 10, 26)


@pytest.fixture
def repository(tmp_path):
    repo = ScheduleRepository(tmp_path / "registration.sqlite")
    lessons = (
        Lesson(DAY, "ИС-22", 1, "1 пара", 1, "09:00", "10:00", "Математика", "Иванов И.И.", "1"),
        Lesson(DAY, "ПД-12", 1, "1 пара", 1, "09:00", "10:00", "Право", "Петров П.П.", "2"),
    )
    repo.import_schedule(ParsedSchedule("schedule.xlsx", DAY, DAY, (DAY,), lessons), sha256="registration")
    repo.save_profile(OTHER, role="student", target="ПД-12", notifications=False)
    return repo


@pytest.fixture
def state():
    return FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=USER, user_id=USER))


def message(text="/start"):
    return SimpleNamespace(text=text, from_user=SimpleNamespace(id=USER), answer=AsyncMock())


def callback(data):
    return SimpleNamespace(
        data=data, from_user=SimpleNamespace(id=USER),
        message=SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock()), answer=AsyncMock(),
    )


def analytics():
    return SimpleNamespace(track=AsyncMock())


def profile(repo, user=USER):
    return repo.get_profile(user)


@pytest.mark.asyncio
async def test_student_registration_and_repeat_start_preserve_profile_and_clear_state(repository, state):
    other_before = profile(repository, OTHER)
    first = message()
    await app.start(first, state, analytics())
    assert [b.callback_data for b in first.answer.call_args.kwargs["reply_markup"].inline_keyboard[0]] == [
        "role:student", "role:teacher"
    ]
    role = callback("role:student")
    await app.choose_student_group(role, repository, state)
    buttons = role.message.answer.call_args.kwargs["reply_markup"].inline_keyboard
    assert {button.callback_data for row in buttons for button in row} == {"group:ИС-22", "group:ПД-12"}
    selected = callback("group:ИС-22")
    await app.save_student_group(selected, repository, analytics())
    assert profile(repository) == {"user_id": USER, "role": "student", "target": "ИС-22", "notifications": True}
    assert "ИС-22" in selected.message.edit_text.call_args.args[0]
    await state.set_state(app.ProfileSetup.choosing_teacher)
    await state.update_data(teacher_matches=["Иванов И.И."])
    again = message()
    await app.start(again, state, analytics())
    assert await state.get_state() is None
    assert await state.get_data() == {}
    assert profile(repository)["target"] == "ИС-22"
    assert profile(repository, OTHER) == other_before
    assert "Кому" in again.answer.call_args.args[0]


@pytest.mark.asyncio
async def test_teacher_registration_and_profile_changes_preserve_notification_setting(repository, state):
    repository.save_profile(USER, role="student", target="ИС-22", notifications=False)
    other_before = profile(repository, OTHER)
    role = callback("role:teacher")
    await app.request_teacher(role, state)
    assert await state.get_state() == app.ProfileSetup.waiting_teacher_query.state
    query = message("иванов")
    await app.find_teacher(query, repository, state)
    assert await state.get_state() == app.ProfileSetup.choosing_teacher.state
    assert (await state.get_data())["teacher_matches"] == ["Иванов И.И."]
    button = query.answer.call_args.kwargs["reply_markup"].inline_keyboard[0][0]
    search_id = (await state.get_data())["teacher_search_id"]
    assert (button.text, button.callback_data) == ("Иванов И.И.", f"teacher:{search_id}:0")
    selected = callback(button.callback_data)
    await app.save_teacher(selected, repository, state, analytics())
    assert profile(repository) == {"user_id": USER, "role": "teacher", "target": "Иванов И.И.", "notifications": False}
    assert "Иванов И.И." in selected.message.edit_text.call_args.args[0]
    assert await state.get_state() is None and await state.get_data() == {}
    await app.save_student_group(callback("group:ПД-12"), repository, analytics())
    assert profile(repository) == {"user_id": USER, "role": "student", "target": "ПД-12", "notifications": False}
    assert profile(repository, OTHER) == other_before


@pytest.mark.asyncio
async def test_teacher_search_without_matches_keeps_query_state_and_existing_profiles(repository, state):
    repository.save_profile(USER, role="student", target="ИС-22")
    before = (profile(repository), profile(repository, OTHER))
    await app.request_teacher(callback("role:teacher"), state)
    query = message("Несуществующий преподаватель")
    await app.find_teacher(query, repository, state)
    assert "не найден" in query.answer.call_args.args[0]
    assert await state.get_state() == app.ProfileSetup.waiting_teacher_query.state
    assert await state.get_data() == {}
    assert (profile(repository), profile(repository, OTHER)) == before


@pytest.mark.asyncio
async def test_no_groups_reports_no_schedule_and_clears_state(tmp_path, state):
    repository = ScheduleRepository(tmp_path / "empty.sqlite")
    await state.set_state(app.ProfileSetup.choosing_teacher)
    await state.update_data(teacher_matches=["Старый список"])
    value = callback("role:student")
    await app.choose_student_group(value, repository, state)
    assert "ещё не загружено" in value.message.answer.call_args.args[0]
    assert await state.get_state() is None and await state.get_data() == {}
    assert profile(repository) is None
    value.answer.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [
    "teacher:test-search:99", "teacher:test-search:1", "teacher:test-search:abc",
    "teacher:test-search:", "teacher:0", "teacher:other-search:0",
])
async def test_invalid_teacher_choices_do_not_change_profile(repository, state, data):
    repository.save_profile(USER, role="student", target="ИС-22")
    before = (profile(repository), profile(repository, OTHER))
    await state.set_state(app.ProfileSetup.choosing_teacher)
    await state.update_data(teacher_matches=["Иванов И.И."], teacher_search_id="test-search")
    value = callback(data)
    await app.save_teacher(value, repository, state, analytics())
    assert value.answer.call_args.kwargs["show_alert"] is True
    assert "устарел" in value.answer.call_args.args[0]
    assert (profile(repository), profile(repository, OTHER)) == before
    assert await state.get_state() == app.ProfileSetup.choosing_teacher.state
    value.message.answer.assert_not_awaited()
    value.message.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_teacher_button_does_not_change_profiles(repository):
    repository.save_profile(USER, role="student", target="ИС-22")
    before = (profile(repository), profile(repository, OTHER))
    value = callback("teacher:0")
    await app.expired_teacher_choice(value)
    assert value.answer.call_args.kwargs["show_alert"] is True
    assert "устарел" in value.answer.call_args.args[0]
    assert (profile(repository), profile(repository, OTHER)) == before
    value.message.answer.assert_not_awaited()
    value.message.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_last_teacher_search_result_can_be_selected(repository, state):
    await app.request_teacher(callback("role:teacher"), state)
    query = message(".")
    await app.find_teacher(query, repository, state)
    buttons = query.answer.call_args.kwargs["reply_markup"].inline_keyboard
    assert [row[0].text for row in buttons] == ["Иванов И.И.", "Петров П.П."]
    selected = callback(buttons[-1][0].callback_data)
    await app.save_teacher(selected, repository, state, analytics())
    assert profile(repository)["target"] == "Петров П.П."
    assert await state.get_state() is None and await state.get_data() == {}


@pytest.mark.asyncio
async def test_unknown_group_callback_must_not_replace_existing_profile(repository):
    repository.save_profile(USER, role="student", target="ИС-22")
    before = (profile(repository), profile(repository, OTHER))
    value = callback("group:Несуществующая")
    await app.save_student_group(value, repository, analytics())
    assert (profile(repository), profile(repository, OTHER)) == before
    assert value.answer.call_args.kwargs["show_alert"] is True
    assert "групп" in value.answer.call_args.args[0].lower()
    value.message.answer.assert_not_awaited()
    value.message.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_negative_teacher_index_must_not_replace_existing_profile(repository, state):
    repository.save_profile(USER, role="student", target="ИС-22")
    before = (profile(repository), profile(repository, OTHER))
    await state.set_state(app.ProfileSetup.choosing_teacher)
    await state.update_data(teacher_matches=["Иванов И.И."], teacher_search_id="test-search")
    value = callback("teacher:test-search:-1")
    await app.save_teacher(value, repository, state, analytics())
    assert (profile(repository), profile(repository, OTHER)) == before
    assert value.answer.call_args.kwargs["show_alert"] is True


@pytest.mark.asyncio
async def test_old_teacher_button_must_not_select_a_new_search_result(repository, state):
    repository.save_profile(USER, role="student", target="ИС-22")
    before = (profile(repository), profile(repository, OTHER))
    await app.request_teacher(callback("role:teacher"), state)
    first_query = message("Иванов")
    await app.find_teacher(first_query, repository, state)
    old_button = first_query.answer.call_args.kwargs["reply_markup"].inline_keyboard[0][0]
    await app.request_teacher(callback("role:teacher"), state)
    await app.find_teacher(message("Петров"), repository, state)
    value = callback(old_button.callback_data)
    await app.save_teacher(value, repository, state, analytics())
    assert (profile(repository), profile(repository, OTHER)) == before
    assert value.answer.call_args.kwargs["show_alert"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["student", "teacher"])
async def test_profile_change_keyboard_reflects_disabled_notifications(repository, state, role):
    repository.save_profile(USER, role="student", target="ИС-22", notifications=False)
    if role == "student":
        value = callback("group:ПД-12")
        await app.save_student_group(value, repository, analytics())
    else:
        await state.set_state(app.ProfileSetup.waiting_teacher_query)
        query = message("Иванов")
        await app.find_teacher(query, repository, state)
        button = query.answer.call_args.kwargs["reply_markup"].inline_keyboard[0][0]
        value = callback(button.callback_data)
        await app.save_teacher(value, repository, state, analytics())
    assert profile(repository)["notifications"] is False
    buttons = value.message.answer.call_args.kwargs["reply_markup"].keyboard
    assert any("Уведомления: выключены" == button.text for row in buttons for button in row)


@pytest.mark.asyncio
async def test_stale_group_button_must_not_replace_profile(repository, state):
    repository.save_profile(USER, role="student", target="ПД-12")
    before = (profile(repository), profile(repository, OTHER))
    selection = callback("role:student")
    await app.choose_student_group(selection, repository, state)
    buttons = selection.message.answer.call_args.kwargs["reply_markup"].inline_keyboard
    old_button = next(button for row in buttons for button in row if button.text == "ИС-22")
    replacement = Lesson(DAY, "ПД-12", 1, "1 пара", 1, "09:00", "10:00", "Право", "Петров П.П.", "2")
    repository.import_schedule(
        ParsedSchedule("updated.xlsx", DAY, DAY, (DAY,), (replacement,)),
        sha256="updated-registration", approval_reason="Test approved update",
    )
    assert repository.list_groups() == ["ПД-12"]
    value = callback(old_button.callback_data)
    await app.save_student_group(value, repository, analytics())
    assert (profile(repository), profile(repository, OTHER)) == before
    assert value.answer.call_args.kwargs["show_alert"] is True
    assert "групп" in value.answer.call_args.args[0].lower()
    value.message.answer.assert_not_awaited()
    value.message.edit_text.assert_not_awaited()
