from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from schedule_bot.bot.app import private_menu_action
from schedule_bot.bot.groups import group_menu_action
from schedule_bot.domain import ParsedSchedule
from schedule_bot.repository import ScheduleRepository


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type,prefix", [("private", "pnav"), ("supergroup", "gnav")])
@pytest.mark.parametrize("action", ["dates", "dates_page_1"])
async def test_date_selector_reaches_past_dates_on_later_pages(
    tmp_path: Path, chat_type: str, prefix: str, action: str
) -> None:
    repository = ScheduleRepository(tmp_path / "schedule.db")
    dates = tuple(date(2026, 9, 1) + timedelta(days=index) for index in range(15))
    repository.import_schedule(
        ParsedSchedule(
            source_filename="01.-15.09.2026.xlsx",
            start_date=dates[0],
            end_date=dates[-1],
            dates=dates,
            lessons=(),
        ),
        sha256="published-dates",
    )
    if chat_type == "private":
        repository.save_profile(123, role="student", target="ИС-22")
    else:
        repository.save_chat_profile(
            -100123,
            chat_type="supergroup",
            chat_title="ИС-22",
            target="ИС-22",
            configured_by=123,
        )

    edits: list[tuple[str, object]] = []

    async def edit_text(text: str, *, reply_markup=None) -> None:
        edits.append((text, reply_markup))

    async def answer(text: str = "", *, show_alert: bool = False) -> None:
        return None

    callback = SimpleNamespace(
        data=f"{prefix}:123:{action}",
        from_user=SimpleNamespace(id=123),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=-100123, type=chat_type),
            edit_text=edit_text,
        ),
        answer=answer,
    )
    settings = SimpleNamespace(timezone="Europe/Moscow")
    if chat_type == "private":
        await private_menu_action(  # type: ignore[arg-type]
            callback, repository, settings, analytics=None
        )
    else:
        await group_menu_action(  # type: ignore[arg-type]
            callback, bot=None, repository=repository, settings=settings, analytics=None
        )

    assert len(edits) == 1
    text, keyboard = edits[0]
    assert "Выберите дату" in text
    callbacks = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    ]
    assert f"{prefix}:123:date_2026-09-13" in callbacks
    assert f"{prefix}:123:date_2026-09-15" in callbacks
    assert f"{prefix}:123:dates_page_0" in callbacks
