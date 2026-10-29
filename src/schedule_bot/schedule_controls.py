"""Schedule view controls shared by interactive replies and queued deliveries."""
from datetime import date

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def callback_data(prefix: str, owner_id: int, action: str) -> str:
    value = f"{prefix}:{owner_id}:{action}"
    if len(value.encode("utf-8")) > 64:
        raise ValueError("Telegram callback data exceeds 64 bytes")
    return value


def schedule_view_keyboard(
    owner_id: int, lesson_date: date, *, as_card: bool, prefix: str = "pnav",
    report_button: InlineKeyboardButton | None = None,
) -> InlineKeyboardMarkup:
    action = "text" if as_card else "card"
    label = "Текст" if as_card else "Карточка"
    rows = [
        [InlineKeyboardButton(text=label, callback_data=callback_data(prefix, owner_id, f"view_{action}_{lesson_date.isoformat()}"))],
    ]
    if report_button is not None:
        rows.append([report_button])
    rows.append([InlineKeyboardButton(text="Меню расписания", callback_data=callback_data(prefix, owner_id, "root"))])
    return InlineKeyboardMarkup(inline_keyboard=rows)
