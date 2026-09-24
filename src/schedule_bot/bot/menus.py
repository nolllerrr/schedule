from __future__ import annotations

from bisect import bisect_left
from datetime import date

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


WEEKDAY_LABELS = (
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
    "Воскресенье",
)
DATE_PAGE_SIZE = 12


def schedule_menu_keyboard(
    prefix: str,
    owner_id: int,
    *,
    include_settings: bool = False,
) -> InlineKeyboardMarkup:
    rows = [
        [
            _button("Сегодня", prefix, owner_id, "today"),
            _button("Завтра", prefix, owner_id, "tomorrow"),
        ],
        [_button("Неделя", prefix, owner_id, "week")],
        [_button("Выбрать дату", prefix, owner_id, "dates")],
    ]
    if include_settings:
        rows.append([_button("Настройки чата", prefix, owner_id, "settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def dates_menu_keyboard(
    prefix: str,
    owner_id: int,
    dates: list[date],
    *,
    page: int = 0,
) -> InlineKeyboardMarkup:
    page_count = max(1, (len(dates) + DATE_PAGE_SIZE - 1) // DATE_PAGE_SIZE)
    page = min(max(page, 0), page_count - 1)
    visible_dates = dates[page * DATE_PAGE_SIZE : (page + 1) * DATE_PAGE_SIZE]
    rows = [
        [
            _button(
                value.strftime("%d.%m.%Y"),
                prefix,
                owner_id,
                f"date_{value.isoformat()}",
            )
        ]
        for value in visible_dates
    ]
    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(
            _button("← Раньше", prefix, owner_id, f"dates_page_{page - 1}")
        )
    if page < page_count - 1:
        navigation.append(
            _button("Позже →", prefix, owner_id, f"dates_page_{page + 1}")
        )
    if navigation:
        rows.append(navigation)
    rows.append([_button("← Назад", prefix, owner_id, "root")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def dates_page_from_action(action: str) -> int | None:
    if action == "dates":
        return 0
    if not action.startswith("dates_page_"):
        return None
    try:
        page = int(action.removeprefix("dates_page_"))
    except ValueError:
        return None
    return page if page >= 0 else None


def initial_dates_page(dates: list[date], today: date) -> int:
    """Open near today, or on the latest page when every date is in the past."""
    if not dates:
        return 0
    index = min(bisect_left(dates, today), len(dates) - 1)
    return index // DATE_PAGE_SIZE


def week_menu_keyboard(
    prefix: str,
    owner_id: int,
    dates: list[date],
) -> InlineKeyboardMarkup:
    rows = [
        [
            _button(
                f"{WEEKDAY_LABELS[value.weekday()]} · {value:%d.%m}",
                prefix,
                owner_id,
                f"weekdate_{value.isoformat()}",
            )
        ]
        for value in dates
    ]
    rows.append([_button("← Назад", prefix, owner_id, "root")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def schedule_result_keyboard(
    prefix: str,
    owner_id: int,
    *,
    include_settings: bool = False,
    back_action: str = "root",
    back_text: str = "Меню расписания",
) -> InlineKeyboardMarkup:
    rows = [[_button(back_text, prefix, owner_id, back_action)]]
    if include_settings:
        rows.append([_button("Настройки чата", prefix, owner_id, "settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def callback_parts(data: str | None, prefix: str) -> tuple[int, str] | None:
    if not data:
        return None
    parts = data.split(":", 2)
    if len(parts) != 3 or parts[0] != prefix:
        return None
    try:
        return int(parts[1]), parts[2]
    except ValueError:
        return None


def callback_data(prefix: str, owner_id: int, action: str) -> str:
    value = f"{prefix}:{owner_id}:{action}"
    if len(value.encode("utf-8")) > 64:
        raise ValueError("Telegram callback data exceeds 64 bytes")
    return value


def _button(
    text: str,
    prefix: str,
    owner_id: int,
    action: str,
) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text,
        callback_data=callback_data(prefix, owner_id, action),
    )
