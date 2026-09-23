from datetime import date
from types import SimpleNamespace

import pytest
from aiogram.enums import ChatMemberStatus, ChatType

from schedule_bot.bot.groups import (
    GROUP_MENU_PREFIX,
    chat_type_value,
    group_menu_action,
    group_selection_keyboard,
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
async def test_group_schedule_button_edits_existing_menu_message(
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
    assert "<b>45</b> ауд. | <b>МДК.01.04.</b>" in edits[0][0]
    assert "—09:00–10:30—" in edits[0][0]
    assert edits[0][1].inline_keyboard
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

    async def unpin_chat_message(chat_id: int, message_id: int) -> None:
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
