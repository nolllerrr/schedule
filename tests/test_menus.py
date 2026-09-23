from datetime import date

import pytest

from schedule_bot.bot.menus import (
    callback_data,
    callback_parts,
    dates_menu_keyboard,
    schedule_menu_keyboard,
    schedule_result_keyboard,
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


def test_schedule_result_has_no_unbounded_day_navigation() -> None:
    keyboard = schedule_result_keyboard("gnav", 123, include_settings=True)
    callbacks = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    ]

    assert callbacks == ["gnav:123:root", "gnav:123:settings"]


def test_callback_parts_rejects_wrong_or_invalid_data() -> None:
    assert callback_parts("gnav:123:week", "gnav") == (123, "week")
    assert callback_parts("pnav:123:week", "gnav") is None
    assert callback_parts("gnav:not-a-number:week", "gnav") is None
    assert callback_parts(None, "gnav") is None


def test_callback_data_enforces_telegram_limit() -> None:
    with pytest.raises(ValueError):
        callback_data("menu", 123, "x" * 64)
