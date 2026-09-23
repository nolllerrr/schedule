from __future__ import annotations

from datetime import date, timedelta

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


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
        [
            _button("Неделя", prefix, owner_id, "week"),
            _button("Следующий день", prefix, owner_id, "next"),
        ],
        [_button("Выбрать дату", prefix, owner_id, "dates")],
    ]
    if include_settings:
        rows.append([_button("Настройки чата", prefix, owner_id, "settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def dates_menu_keyboard(
    prefix: str,
    owner_id: int,
    dates: list[date],
) -> InlineKeyboardMarkup:
    rows = [
        [
            _button(
                value.strftime("%d.%m.%Y"),
                prefix,
                owner_id,
                f"date_{value.isoformat()}",
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
    current_date: date | None = None,
    include_settings: bool = False,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if current_date is not None:
        rows.append(
            [
                _button(
                    "← День назад",
                    prefix,
                    owner_id,
                    f"date_{(current_date - timedelta(days=1)).isoformat()}",
                ),
                _button(
                    "День вперёд →",
                    prefix,
                    owner_id,
                    f"date_{(current_date + timedelta(days=1)).isoformat()}",
                ),
            ]
        )
    rows.append([_button("Меню расписания", prefix, owner_id, "root")])
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
