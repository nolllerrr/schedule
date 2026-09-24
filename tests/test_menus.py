from datetime import date, timedelta

import pytest

from schedule_bot.bot.menus import (
    callback_data,
    callback_parts,
    dates_page_from_action,
    dates_menu_keyboard,
    initial_dates_page,
    schedule_menu_keyboard,
    schedule_result_keyboard,
    week_menu_keyboard,
)


def test_schedule_menu_callbacks_include_owner() -> None:
    keyboard = schedule_menu_keyboard("gnav", 123, include_settings=True)
    callbacks = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    ]

    assert "gnav:123:today" in callbacks
    assert "gnav:123:tomorrow" in callbacks
    assert "gnav:123:week" in callbacks
    assert "gnav:123:settings" in callbacks
    assert all("next" not in str(value) for value in callbacks)


def test_dates_menu_has_date_and_back_callback() -> None:
    keyboard = dates_menu_keyboard(
        "pnav", 456, [date(2026, 9, 23), date(2026, 9, 24)]
    )

    assert keyboard.inline_keyboard[0][0].callback_data == "pnav:456:date_2026-09-23"
    assert keyboard.inline_keyboard[-1][0].callback_data == "pnav:456:root"


def test_dates_menu_pages_through_every_published_date() -> None:
    dates = [date(2026, 9, 1) + timedelta(days=index) for index in range(15)]

    first = dates_menu_keyboard("pnav", 456, dates)
    assert first.inline_keyboard[0][0].text == "01.09.2026"
    assert first.inline_keyboard[11][0].text == "12.09.2026"
    assert first.inline_keyboard[12][0].callback_data == "pnav:456:dates_page_1"

    second = dates_menu_keyboard("pnav", 456, dates, page=1)
    assert second.inline_keyboard[0][0].text == "13.09.2026"
    assert second.inline_keyboard[2][0].text == "15.09.2026"
    assert second.inline_keyboard[3][0].callback_data == "pnav:456:dates_page_0"
    assert second.inline_keyboard[-1][0].callback_data == "pnav:456:root"
    assert dates_menu_keyboard("pnav", 456, dates, page=99) == second

    assert dates_page_from_action("dates") == 0
    assert dates_page_from_action("dates_page_1") == 1
    assert dates_page_from_action("dates_page_-1") is None
    assert dates_page_from_action("dates_page_invalid") is None
    assert initial_dates_page(dates, date(2026, 9, 24)) == 1
    assert initial_dates_page(dates, date(2026, 9, 3)) == 0
    assert initial_dates_page(dates, date(2026, 9, 13)) == 1


def test_schedule_result_has_no_unbounded_day_navigation() -> None:
    keyboard = schedule_result_keyboard("gnav", 123, include_settings=True)
    callbacks = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    ]

    assert callbacks == ["gnav:123:root", "gnav:123:settings"]


def test_week_menu_shows_weekdays_and_returns_to_week_selector() -> None:
    keyboard = week_menu_keyboard(
        "gnav",
        123,
        [date(2026, 9, 23), date(2026, 9, 24)],
    )

    assert keyboard.inline_keyboard[0][0].text == "Среда · 23.09"
    assert keyboard.inline_keyboard[0][0].callback_data == (
        "gnav:123:weekdate_2026-09-23"
    )
    assert keyboard.inline_keyboard[1][0].text == "Четверг · 24.09"
    assert keyboard.inline_keyboard[-1][0].callback_data == "gnav:123:root"

    result = schedule_result_keyboard(
        "gnav",
        123,
        back_action="week",
        back_text="← К дням недели",
    )
    assert result.inline_keyboard[0][0].callback_data == "gnav:123:week"


def test_callback_parts_rejects_wrong_or_invalid_data() -> None:
    assert callback_parts("gnav:123:week", "gnav") == (123, "week")
    assert callback_parts("pnav:123:week", "gnav") is None
    assert callback_parts("gnav:not-a-number:week", "gnav") is None
    assert callback_parts(None, "gnav") is None


def test_callback_data_enforces_telegram_limit() -> None:
    with pytest.raises(ValueError):
        callback_data("menu", 123, "x" * 64)
