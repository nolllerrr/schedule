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
) -> InlineKeyboardMarkup:
    action = "text" if as_card else "card"
    label = "Текст" if as_card else "Карточка"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=callback_data(prefix, owner_id, f"view_{action}_{lesson_date.isoformat()}"))],
        [InlineKeyboardButton(text="Меню расписания", callback_data=callback_data(prefix, owner_id, "root"))],
    ])
