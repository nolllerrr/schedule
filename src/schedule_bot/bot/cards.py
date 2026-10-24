"""On-demand card delivery; profile authorization stays in the menu handlers."""
from __future__ import annotations

import logging
import re
from datetime import date

from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile, CallbackQuery

from schedule_bot.async_repository import repository_async
from schedule_bot.card_renderer import CardNotSupported
from schedule_bot.card_service import CardBusy, CardTimeout, get_card_service
from schedule_bot.freshness import with_freshness


logger = logging.getLogger(__name__)


def card_action(role: str, lesson_date: date) -> str | None:
    # Teacher schedules may contain simultaneous groups in one numbered slot.
    return f"card_{lesson_date.isoformat()}" if role == "student" else None


def card_date(action: str) -> date | None:
    match = re.fullmatch(r"card_(\d{4}-\d{2}-\d{2})", action)
    if match is None:
        return None
    try:
        return date.fromisoformat(match[1])
    except ValueError:
        return None


async def _notice(callback: CallbackQuery, text: str) -> None:
    if callback.message is None:
        return
    try:
        await callback.message.answer(text, parse_mode=None)
    except TelegramAPIError as error:
        logger.warning("Card explanation could not be sent (%s)", type(error).__name__)


async def send_schedule_card(
    callback: CallbackQuery,
    repository,
    settings,
    *,
    role: str,
    target: str,
    action: str,
) -> None:
    """Fetch current SQLite lessons and send a new photo without editing the menu."""
    lesson_date = card_date(action)
    if lesson_date is None or callback.message is None:
        await callback.answer("Кнопка устарела.", show_alert=True)
        return
    if role != "student":
        await callback.answer(
            "Карточки пока доступны для учебных групп. Используйте текстовое расписание.",
            show_alert=True,
        )
        return
    # Acknowledge before the SQLite read/render: the menu stays usable throughout.
    try:
        await callback.answer()
    except TelegramAPIError as error:
        logger.info("Card callback acknowledgement failed (%s)", type(error).__name__)
    try:
        lessons = await repository_async(repository).lessons_for(
            role="student", target=target, lesson_date=lesson_date,
        )
        if not lessons:
            await _notice(
                callback,
                "Карточка недоступна: на эту дату нет сохранённых занятий "
                "или расписание ещё не опубликовано. Проверьте текстовое расписание.",
            )
            return
        png = await get_card_service().get_card(
            role=role, target=target, lesson_date=lesson_date, lessons=lessons,
        )
        caption = await with_freshness(
            f"Расписание · {lesson_date:%d.%m.%Y}", repository, settings,
        )
        await callback.message.answer_photo(
            BufferedInputFile(png, filename=f"schedule-{lesson_date.isoformat()}.png"),
            caption=caption,
            parse_mode=None,
        )
    except CardNotSupported:
        await _notice(
            callback,
            "Эти занятия нельзя точно разместить на карточке без потери сведений. "
            "Используйте текстовое расписание в исходном сообщении.",
        )
    except CardBusy:
        await _notice(
            callback,
            "Сейчас создаются другие карточки. Попробуйте чуть позже; "
            "текстовое расписание доступно в исходном сообщении.",
        )
    except CardTimeout:
        await _notice(
            callback,
            "Создание карточки заняло слишком много времени. Попробуйте позже "
            "или используйте текстовое расписание.",
        )
    except TelegramAPIError as error:
        logger.warning("Schedule card delivery failed (%s)", type(error).__name__)
        await _notice(
            callback,
            "Не удалось отправить карточку. Текстовое расписание осталось "
            "в исходном сообщении; попробуйте позже.",
        )
    except Exception as error:
        # Do not let rendering/font failures affect the rest of the bot, and do
        # not log exception payloads containing lesson fields or Telegram URLs.
        logger.warning("Schedule card unavailable (%s)", type(error).__name__)
        await _notice(
            callback,
            "Карточка временно недоступна. Используйте текстовое расписание "
            "в исходном сообщении.",
        )
