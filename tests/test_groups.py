from types import SimpleNamespace

import pytest
from aiogram.enums import ChatType

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
