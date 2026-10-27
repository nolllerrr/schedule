from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.usefixtures("text_schedule_media")
from aiogram.enums import ChatMemberStatus, ChatType

from schedule_bot.bot.groups import (
    GROUP_MENU_PREFIX,
    chat_type_value,
    group_menu_action,
    group_selection_keyboard,
    save_group_setup,
)
from schedule_bot.parser import ExcelScheduleParser
from schedule_bot.repository import ScheduleRepository


def test_chat_type_value_accepts_string_from_aiogram_model() -> None:
    assert chat_type_value("supergroup") == "supergroup"


def test_chat_type_value_accepts_enum_for_compatibility() -> None:
    assert chat_type_value(ChatType.GROUP) == "group"


def test_group_selection_buttons_belong_to_menu_owner() -> None:
    keyboard = group_selection_keyboard(["ИС-22"], 123)

    assert keyboard.inline_keyboard[0][0].callback_data == "group_setup:123:ИС-22"


@pytest.mark.asyncio
async def test_changing_group_replaces_pinned_schedule(
    tmp_path,
    schedule_workbook,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "schedule_bot.bot.groups._now",
        lambda settings: datetime(2026, 9, 22, 9, 0),
    )
    repository = ScheduleRepository(tmp_path / "schedule.db")
    repository.import_schedule(
        ExcelScheduleParser().parse(schedule_workbook),
        sha256="change-group-pin",
    )
    repository.save_chat_profile(
        -100321,
        chat_type="supergroup",
        chat_title="Тестовая группа",
        target="ПД-12",
        configured_by=123,
        pin_enabled=True,
    )
    repository.set_chat_last_message(-100321, 50)
    unpinned: list[tuple[int, int]] = []
    sent: list[tuple[int, str]] = []
    pinned: list[tuple[int, int]] = []
    answers: list[tuple[str, bool]] = []

    async def get_chat_member(chat_id: int, user_id: int):
        return SimpleNamespace(
            status=ChatMemberStatus.ADMINISTRATOR, can_pin_messages=True
        )

    async def unpin_chat_message(chat_id: int, *, message_id: int) -> None:
        unpinned.append((chat_id, message_id))

    async def send_message(chat_id: int, text: str, **kwargs):
        sent.append((chat_id, text))
        return SimpleNamespace(message_id=51)

    async def pin_chat_message(
        *, chat_id: int, message_id: int, disable_notification: bool
    ) -> None:
        pinned.append((chat_id, message_id))

    async def edit_text(text: str, *, reply_markup=None) -> None:
        return None

    async def answer(text: str = "", *, show_alert: bool = False) -> None:
        answers.append((text, show_alert))

    async def track(event_type: str, **kwargs) -> None:
        return None

    bot = SimpleNamespace(
        id=999,
        get_chat_member=get_chat_member,
        unpin_chat_message=unpin_chat_message,
        send_message=send_message,
        pin_chat_message=pin_chat_message,
    )
    callback = SimpleNamespace(
        data="group_setup:123:ИС-22",
        from_user=SimpleNamespace(id=123),
        message=SimpleNamespace(
            chat=SimpleNamespace(
                id=-100321,
                type="supergroup",
                title="Тестовая группа",
            ),
            edit_text=edit_text,
        ),
        answer=answer,
    )

    await save_group_setup(  # type: ignore[arg-type]
        callback,
        bot=bot,
        repository=repository,
        settings=SimpleNamespace(timezone="Europe/Moscow"),
        analytics=SimpleNamespace(track=track),
    )

    profile = repository.get_chat_profile(-100321)
    assert profile["target"] == "ИС-22"
    assert profile["pin_enabled"] is True
    assert profile["last_pinned_message_id"] == 51
    assert unpinned == [(-100321, 50)]
    assert len(sent) == 1
    assert "МДК.01.04." in sent[0][1]
    assert pinned == [(-100321, 51)]
    assert answers == [("Группа изменена, новое расписание закреплено.", False)]


@pytest.mark.asyncio
async def test_changing_group_removes_stale_pin_when_setting_is_off(
    tmp_path,
    schedule_workbook,
) -> None:
    repository = ScheduleRepository(tmp_path / "schedule.db")
    repository.import_schedule(
        ExcelScheduleParser().parse(schedule_workbook),
        sha256="change-group-stale-pin",
    )
    repository.save_chat_profile(
        -100654,
        chat_type="supergroup",
        chat_title="Тестовая группа",
        target="ПД-12",
        configured_by=123,
        pin_enabled=False,
    )
    repository.set_chat_last_message(-100654, 60)
    unpinned: list[tuple[int, int]] = []
    sent: list[tuple[int, str]] = []

    async def get_chat_member(chat_id: int, user_id: int):
        return SimpleNamespace(status=ChatMemberStatus.ADMINISTRATOR)

    async def unpin_chat_message(chat_id: int, *, message_id: int) -> None:
        unpinned.append((chat_id, message_id))

    async def send_message(chat_id: int, text: str, **kwargs):
        sent.append((chat_id, text))

    async def edit_text(text: str, *, reply_markup=None) -> None:
        return None

    async def answer(text: str = "", *, show_alert: bool = False) -> None:
        return None

    async def track(event_type: str, **kwargs) -> None:
        return None

    callback = SimpleNamespace(
        data="group_setup:123:ИС-22",
        from_user=SimpleNamespace(id=123),
        message=SimpleNamespace(
            chat=SimpleNamespace(
                id=-100654,
                type="supergroup",
                title="Тестовая группа",
            ),
            edit_text=edit_text,
        ),
        answer=answer,
    )
    bot = SimpleNamespace(
        get_chat_member=get_chat_member,
        unpin_chat_message=unpin_chat_message,
        send_message=send_message,
    )

    await save_group_setup(  # type: ignore[arg-type]
        callback,
        bot=bot,
        repository=repository,
        settings=SimpleNamespace(timezone="Europe/Moscow"),
        analytics=SimpleNamespace(track=track),
    )

    profile = repository.get_chat_profile(-100654)
    assert profile["target"] == "ИС-22"
    assert profile["pin_enabled"] is False
    assert profile["last_pinned_message_id"] is None
    assert unpinned == [(-100654, 60)]
    assert sent == []


@pytest.mark.asyncio
async def test_group_menu_rejects_click_from_another_user() -> None:
    answers: list[tuple[str, bool]] = []

    async def answer(text: str = "", *, show_alert: bool = False) -> None:
        answers.append((text, show_alert))

    callback = SimpleNamespace(
        data=f"{GROUP_MENU_PREFIX}:123:today",
        from_user=SimpleNamespace(id=456),
        message=SimpleNamespace(),
        answer=answer,
    )

    await group_menu_action(  # type: ignore[arg-type]
        callback,
        bot=None,
        repository=None,
        settings=None,
        analytics=None,
    )

    assert answers == [
        (
            "Это меню открыл другой пользователь. Отправьте /menu, чтобы открыть своё.",
            True,
        )
    ]


@pytest.mark.asyncio
async def test_group_schedule_button_sends_card_and_removes_menu(
    tmp_path,
    schedule_workbook,
) -> None:
    repository = ScheduleRepository(tmp_path / "schedule.db")
    parsed = ExcelScheduleParser().parse(schedule_workbook)
    repository.import_schedule(parsed, sha256="group-menu")
    repository.save_chat_profile(
        -100123,
        chat_type="supergroup",
        chat_title="ИС-22",
        target="ИС-22",
        configured_by=123,
    )
    edits: list[tuple[str, object]] = []
    answers: list[tuple[str, bool]] = []
    events: list[str] = []

    async def edit_text(text: str, *, reply_markup=None) -> None:
        edits.append((text, reply_markup))

    async def answer(text: str = "", *, show_alert: bool = False) -> None:
        answers.append((text, show_alert))

    async def track(event_type: str, **kwargs) -> None:
        events.append(event_type)

    callback = SimpleNamespace(
        data=f"{GROUP_MENU_PREFIX}:123:date_2026-09-22",
        from_user=SimpleNamespace(id=123),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=-100123, type="supergroup"),
            edit_text=edit_text, answer_photo=AsyncMock(), answer=AsyncMock(), delete=AsyncMock(),
        ),
        answer=answer,
    )

    await group_menu_action(  # type: ignore[arg-type]
        callback,
        bot=None,
        repository=repository,
        settings=SimpleNamespace(timezone="Europe/Moscow"),
        analytics=SimpleNamespace(track=track),
    )

    assert not edits
    callback.message.answer_photo.assert_awaited_once()
    callback.message.delete.assert_awaited_once()
    assert callback.message.answer_photo.call_args.args[0].filename == "schedule-2026-09-22.png"
    assert callback.message.answer_photo.call_args.kwargs["reply_markup"].inline_keyboard[0][0].text == "Текст"
    assert answers == [("", False)]
    assert events == ["schedule_requested"]


@pytest.mark.asyncio
async def test_week_button_shows_day_buttons_instead_of_full_schedule(
    tmp_path,
    schedule_workbook,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "schedule_bot.bot.groups._today",
        lambda settings: date(2026, 9, 21),
    )
    repository = ScheduleRepository(tmp_path / "schedule.db")
    repository.import_schedule(
        ExcelScheduleParser().parse(schedule_workbook),
        sha256="week-menu",
    )
    repository.save_chat_profile(
        -100456,
        chat_type="supergroup",
        chat_title="ПД-12",
        target="ПД-12",
        configured_by=123,
    )
    edits: list[tuple[str, object]] = []

    async def edit_text(text: str, *, reply_markup=None) -> None:
        edits.append((text, reply_markup))

    async def answer(text: str = "", *, show_alert: bool = False) -> None:
        return None

    async def track(event_type: str, **kwargs) -> None:
        raise AssertionError("Opening the week selector is not a schedule view")

    callback = SimpleNamespace(
        data=f"{GROUP_MENU_PREFIX}:123:week",
        from_user=SimpleNamespace(id=123),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=-100456, type="supergroup"),
            edit_text=edit_text,
        ),
        answer=answer,
    )

    await group_menu_action(  # type: ignore[arg-type]
        callback,
        bot=None,
        repository=repository,
        settings=SimpleNamespace(timezone="Europe/Moscow"),
        analytics=SimpleNamespace(track=track),
    )

    assert len(edits) == 1
    text, keyboard = edits[0]
    assert "Выберите день:" in text
    assert "1️⃣" not in text
    labels = [row[0].text for row in keyboard.inline_keyboard]
    assert labels[:3] == [
        "Понедельник · 21.09",
        "Вторник · 22.09",
        "Среда · 23.09",
    ]


@pytest.mark.asyncio
async def test_disabling_pin_from_settings_unpins_bot_message(tmp_path) -> None:
    repository = ScheduleRepository(tmp_path / "schedule.db")
    repository.save_chat_profile(
        -100999,
        chat_type="supergroup",
        chat_title="ИС-22",
        target="ИС-22",
        configured_by=123,
        pin_enabled=True,
    )
    repository.set_chat_last_message(-100999, 77)
    unpinned: list[tuple[int, int]] = []
    edits: list[str] = []

    async def get_chat_member(chat_id: int, user_id: int):
        return SimpleNamespace(status=ChatMemberStatus.ADMINISTRATOR)

    async def unpin_chat_message(
        chat_id: int,
        *,
        message_id: int,
    ) -> None:
        unpinned.append((chat_id, message_id))

    async def edit_text(text: str, *, reply_markup=None) -> None:
        edits.append(text)

    async def answer(text: str = "", *, show_alert: bool = False) -> None:
        return None

    async def track(event_type: str, **kwargs) -> None:
        return None

    callback = SimpleNamespace(
        data=f"{GROUP_MENU_PREFIX}:123:toggle_pin",
        from_user=SimpleNamespace(id=123),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=-100999, type="supergroup"),
            edit_text=edit_text,
        ),
        answer=answer,
    )
    bot = SimpleNamespace(
        get_chat_member=get_chat_member,
        unpin_chat_message=unpin_chat_message,
    )

    await group_menu_action(  # type: ignore[arg-type]
        callback,
        bot=bot,
        repository=repository,
        settings=None,
        analytics=SimpleNamespace(track=track),
    )

    profile = repository.get_chat_profile(-100999)
    assert profile["pin_enabled"] is False
    assert profile["last_pinned_message_id"] is None
    assert unpinned == [(-100999, 77)]
    assert "Закрепление: выключено" in edits[0]


@pytest.mark.asyncio
async def test_enabling_pin_publishes_separate_schedule_message(
    tmp_path,
    schedule_workbook,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "schedule_bot.bot.groups._now",
        lambda settings: datetime(2026, 9, 22, 9, 0),
    )
    repository = ScheduleRepository(tmp_path / "schedule.db")
    repository.import_schedule(
        ExcelScheduleParser().parse(schedule_workbook),
        sha256="enable-pin",
    )
    repository.save_chat_profile(
        -100777,
        chat_type="supergroup",
        chat_title="ПД-12",
        target="ПД-12",
        configured_by=123,
        pin_enabled=False,
    )
    sent: list[tuple[int, str]] = []
    pinned: list[tuple[int, int]] = []
    answers: list[tuple[str, bool]] = []

    async def get_chat_member(chat_id: int, user_id: int):
        return SimpleNamespace(
            status=ChatMemberStatus.ADMINISTRATOR,
            can_pin_messages=True,
        )

    async def send_message(chat_id: int, text: str, **kwargs):
        sent.append((chat_id, text))
        return SimpleNamespace(message_id=88)

    async def pin_chat_message(
        *,
        chat_id: int,
        message_id: int,
        disable_notification: bool,
    ) -> None:
        pinned.append((chat_id, message_id))

    async def edit_text(text: str, *, reply_markup=None) -> None:
        return None

    async def answer(text: str = "", *, show_alert: bool = False) -> None:
        answers.append((text, show_alert))

    async def track(event_type: str, **kwargs) -> None:
        return None

    bot = SimpleNamespace(
        id=999,
        get_chat_member=get_chat_member,
        send_message=send_message,
        pin_chat_message=pin_chat_message,
    )
    callback = SimpleNamespace(
        data=f"{GROUP_MENU_PREFIX}:123:toggle_pin",
        from_user=SimpleNamespace(id=123),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=-100777, type="supergroup"),
            edit_text=edit_text,
        ),
        answer=answer,
    )

    await group_menu_action(  # type: ignore[arg-type]
        callback,
        bot=bot,
        repository=repository,
        settings=SimpleNamespace(timezone="Europe/Moscow"),
        analytics=SimpleNamespace(track=track),
    )

    profile = repository.get_chat_profile(-100777)
    assert profile["pin_enabled"] is True
    assert profile["last_pinned_message_id"] == 88
    assert len(sent) == 1
    assert "Теория гос. и права" in sent[0][1]
    assert pinned == [(-100777, 88)]
    assert answers == [("Актуальное расписание опубликовано и закреплено.", False)]


def _database_snapshot(repository):
    with repository._connect() as connection:
        return list(connection.iterdump())


@pytest.fixture
def configured_chat(tmp_path):
    repository = ScheduleRepository(tmp_path / "permissions.db")
    repository.save_chat_profile(
        -100321, chat_type="supergroup", chat_title="Test", target="ПД-12",
        configured_by=123, notifications=True, pin_enabled=True,
    )
    repository.set_chat_last_message(-100321, 50)
    repository.save_chat_profile(
        -100322, chat_type="group", chat_title="Other", target="ИС-22",
        configured_by=777, notifications=False, pin_enabled=False,
    )
    return repository


def _permission_bot(status):
    return SimpleNamespace(
        get_chat_member=AsyncMock(return_value=SimpleNamespace(status=status)),
        send_message=AsyncMock(), pin_chat_message=AsyncMock(),
        unpin_chat_message=AsyncMock(), delete_message=AsyncMock(),
    )


def _permission_callback(data, user_id=123):
    return SimpleNamespace(
        data=data, from_user=SimpleNamespace(id=user_id), answer=AsyncMock(),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=-100321, type="supergroup", title="Test"),
            edit_text=AsyncMock(), answer=AsyncMock(), delete=AsyncMock(),
        ),
    )


def _assert_no_forbidden_effects(repository, before, bot, callback, analytics):
    assert _database_snapshot(repository) == before
    bot.send_message.assert_not_awaited()
    bot.pin_chat_message.assert_not_awaited()
    bot.unpin_chat_message.assert_not_awaited()
    bot.delete_message.assert_not_awaited()
    callback.message.edit_text.assert_not_awaited()
    callback.message.answer.assert_not_awaited()
    callback.message.delete.assert_not_awaited()
    analytics.track.assert_not_awaited()


@pytest.mark.asyncio
async def test_member_cannot_start_setup(configured_chat) -> None:
    from schedule_bot.bot.groups import setup_group

    before = _database_snapshot(configured_chat)
    bot = _permission_bot(ChatMemberStatus.MEMBER)
    message = SimpleNamespace(
        chat=SimpleNamespace(id=-100321), from_user=SimpleNamespace(id=123),
        answer=AsyncMock(),
    )
    await setup_group(message, bot, configured_chat)
    message.answer.assert_awaited_once_with(
        "Эта настройка доступна только администраторам чата."
    )
    bot.get_chat_member.assert_awaited_once_with(-100321, 123)
    assert _database_snapshot(configured_chat) == before
    bot.send_message.assert_not_awaited()
    bot.pin_chat_message.assert_not_awaited()
    bot.unpin_chat_message.assert_not_awaited()
    bot.delete_message.assert_not_awaited()


@pytest.mark.parametrize("action", ["settings", "change_group", "toggle_notifications", "toggle_pin"])
@pytest.mark.asyncio
async def test_member_cannot_change_settings(configured_chat, action) -> None:
    before = _database_snapshot(configured_chat)
    bot = _permission_bot(ChatMemberStatus.MEMBER)
    callback = _permission_callback(f"gnav:123:{action}")
    analytics = SimpleNamespace(track=AsyncMock())
    await group_menu_action(callback, bot, configured_chat, None, analytics)
    callback.answer.assert_awaited_once_with(
        "Настройки доступны только администраторам чата.", show_alert=True
    )
    bot.get_chat_member.assert_awaited_once_with(-100321, 123)
    _assert_no_forbidden_effects(configured_chat, before, bot, callback, analytics)


@pytest.mark.asyncio
async def test_member_cannot_select_group(configured_chat) -> None:
    before = _database_snapshot(configured_chat)
    bot = _permission_bot(ChatMemberStatus.MEMBER)
    callback = _permission_callback("group_setup:123:ИС-22")
    analytics = SimpleNamespace(track=AsyncMock())
    await save_group_setup(callback, bot, configured_chat, None, analytics)
    callback.answer.assert_awaited_once_with(
        "Настраивать бота может только администратор.", show_alert=True
    )
    bot.get_chat_member.assert_awaited_once_with(-100321, 123)
    _assert_no_forbidden_effects(configured_chat, before, bot, callback, analytics)


@pytest.mark.parametrize("status", [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR])
@pytest.mark.asyncio
async def test_administrators_can_setup_and_toggle_notifications(
    configured_chat, schedule_workbook, status
) -> None:
    from schedule_bot.bot.groups import setup_group

    configured_chat.import_schedule(ExcelScheduleParser().parse(schedule_workbook), sha256="permissions")
    bot = _permission_bot(status)
    message = SimpleNamespace(
        chat=SimpleNamespace(id=-100321), from_user=SimpleNamespace(id=123),
        answer=AsyncMock(),
    )
    await setup_group(message, bot, configured_chat)
    assert message.answer.await_args.args[0] == "Выберите учебную группу для этого чата:"
    assert message.answer.await_args.kwargs["reply_markup"].inline_keyboard

    callback = _permission_callback("gnav:123:toggle_notifications")
    analytics = SimpleNamespace(track=AsyncMock())
    await group_menu_action(callback, bot, configured_chat, None, analytics)
    assert configured_chat.get_chat_profile(-100321)["notifications"] is False
    callback.message.edit_text.assert_awaited_once()
    callback.answer.assert_awaited_once()
    analytics.track.assert_awaited_once()


@pytest.mark.parametrize(
    ("handler", "data", "user_id", "notice", "admin_check"),
    [
        ("menu", "gnav:123:toggle_notifications", 456,
         "Это меню открыл другой пользователь. Отправьте /menu, чтобы открыть своё.", False),
        ("menu", "gnav:123:toggle_pin", 456,
         "Это меню открыл другой пользователь. Отправьте /menu, чтобы открыть своё.", False),
        ("menu", "gnav:123:change_group", 456,
         "Это меню открыл другой пользователь. Отправьте /menu, чтобы открыть своё.", False),
        ("setup", "group_setup:123:ИС-22", 456,
         "Выбирать группу может только тот, кто открыл это меню.", True),
        ("menu", "gnav:not-a-user:toggle_pin", 123, "Кнопка устарела.", False),
        ("menu", "gnav:123", 123, "Кнопка устарела.", False),
        ("menu", "gnav:123:unknown", 123, "Кнопка устарела.", False),
        ("menu", "gnav:123:date_invalid", 123, "Кнопка устарела.", False),
        ("setup", "group_setup:not-a-user:ИС-22", 123,
         "Это меню настройки устарело. Выполните /setup ещё раз.", True),
        ("setup", "group_setup:123", 123,
         "Это меню настройки устарело. Выполните /setup ещё раз.", True),
        ("setup", "group_setup:123:missing", 123, "Эта группа больше не найдена.", True),
    ],
)
@pytest.mark.asyncio
async def test_foreign_and_invalid_callbacks_have_no_side_effects(
    configured_chat, handler, data, user_id, notice, admin_check
) -> None:
    before = _database_snapshot(configured_chat)
    bot = _permission_bot(ChatMemberStatus.ADMINISTRATOR)
    callback = _permission_callback(data, user_id)
    analytics = SimpleNamespace(track=AsyncMock())
    if handler == "setup":
        await save_group_setup(callback, bot, configured_chat, None, analytics)
    else:
        await group_menu_action(callback, bot, configured_chat, None, analytics)
    callback.answer.assert_awaited_once_with(notice, show_alert=True)
    if admin_check:
        bot.get_chat_member.assert_awaited_once_with(-100321, user_id)
    else:
        bot.get_chat_member.assert_not_awaited()
    _assert_no_forbidden_effects(configured_chat, before, bot, callback, analytics)


@pytest.mark.parametrize("status", [ChatMemberStatus.LEFT, ChatMemberStatus.KICKED])
@pytest.mark.asyncio
async def test_bot_removal_deactivates_only_its_chat(configured_chat, status) -> None:
    from schedule_bot.bot.groups import bot_membership_changed

    before = configured_chat.get_chat_profile(-100321)
    other = configured_chat.get_chat_profile(-100322)
    event = SimpleNamespace(
        chat=SimpleNamespace(id=-100321, type="supergroup"),
        new_chat_member=SimpleNamespace(status=status),
    )
    await bot_membership_changed(event, configured_chat)
    after = configured_chat.get_chat_profile(-100321)
    assert after["active"] is False
    assert {k: v for k, v in after.items() if k not in {"active", "updated_at"}} == {
        k: v for k, v in before.items() if k not in {"active", "updated_at"}
    }
    assert configured_chat.get_chat_profile(-100322) == other
    assert all(chat["chat_id"] != -100321 for chat in configured_chat.chat_subscribers())
