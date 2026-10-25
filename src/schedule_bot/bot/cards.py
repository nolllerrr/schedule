"""On-demand card delivery; profile authorization stays in the menu handlers."""
from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from datetime import date

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import (
    BufferedInputFile, CallbackQuery, InputMediaPhoto, InputRichBlockPhoto,
    InputRichMessage, Message, RichBlockCaption,
)

from schedule_bot.async_repository import repository_async
from schedule_bot.bot.menus import schedule_view_keyboard
from schedule_bot.card_renderer import CardNotSupported
from schedule_bot.card_service import CardBusy, CardTimeout, get_card_service
from schedule_bot.domain import Lesson
from schedule_bot.freshness import with_freshness
from schedule_bot.message_utils import answer_in_chunks
from schedule_bot.presentation import format_schedule


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


def schedule_view_action(action: str) -> tuple[str, date] | None:
    match = re.fullmatch(r"view_(card|text)_(\d{4}-\d{2}-\d{2})", action)
    if match is None:
        return None
    try:
        return match[1], date.fromisoformat(match[2])
    except ValueError:
        return None


async def _rich_card(
    lessons: Sequence[Lesson], target: str, lesson_date: date, repository, settings,
) -> InputRichMessage:
    png = await get_card_service().get_card(
        role="student", target=target, lesson_date=lesson_date, lessons=lessons,
    )
    caption = await _card_caption(repository, settings)
    return InputRichMessage(blocks=[InputRichBlockPhoto(
        photo=InputMediaPhoto(media=BufferedInputFile(
            png, filename=f"schedule-{lesson_date.isoformat()}.png",
        )),
        caption=RichBlockCaption(text=caption) if caption else None,
    )])


async def _card_caption(repository, settings) -> str | None:
    # The date is already printed inside the PNG. Only source warnings belong
    # underneath it; a fresh schedule has no caption at all.
    return (await with_freshness("", repository, settings)).strip() or None


async def _schedule_text(
    lessons: Sequence[Lesson], target: str, lesson_date: date, repository, settings,
) -> str:
    return await with_freshness(
        format_schedule(lessons, target, lesson_date, "student"), repository, settings,
    )


def _rich_text(text: str) -> InputRichMessage:
    # Rich HTML preserves line breaks through <br>, and the existing formatter
    # already escapes all schedule fields before adding its own HTML tags.
    return InputRichMessage(html="<p>" + text.replace("\n", "<br>") + "</p>", skip_entity_detection=True)


async def send_current_schedule_view(
    message: Message, repository, settings, *, owner_id: int, target: str,
    lesson_date: date, lessons: Sequence[Lesson],
) -> None:
    """Open the current day as a card, keeping a complete text fallback."""
    as_card = True
    text = None
    try:
        content = await _rich_card(lessons, target, lesson_date, repository, settings)
    except (CardNotSupported, CardBusy, CardTimeout):
        content = None
    except Exception as error:
        logger.warning("Current schedule card unavailable (%s)", type(error).__name__)
        content = None
    if content is None:
        as_card = False
        text = await _schedule_text(lessons, target, lesson_date, repository, settings)
        content = _rich_text(text)
    try:
        await message.answer_rich(
            content, reply_markup=schedule_view_keyboard(owner_id, lesson_date, as_card=as_card),
        )
        return
    except TelegramAPIError as error:
        logger.warning("Current schedule view delivery failed (%s)", type(error).__name__)
    if text is None:
        text = await _schedule_text(lessons, target, lesson_date, repository, settings)
    # Ordinary text also remains editable by editMessageText. Long unsupported
    # schedules keep the established chunking instead of losing any fields.
    await answer_in_chunks(
        message, text, reply_markup=schedule_view_keyboard(owner_id, lesson_date, as_card=False),
    )


async def _view_notice(callback: CallbackQuery, text: str = "") -> None:
    try:
        await callback.answer(text, show_alert=bool(text))
    except TelegramAPIError as error:
        logger.info("Schedule view acknowledgement failed (%s)", type(error).__name__)


async def switch_schedule_view(
    callback: CallbackQuery, repository, settings, *, owner_id: int,
    role: str, target: str, action: str,
) -> None:
    """Edit one rich/text message in either direction; never replace or delete it."""
    parsed = schedule_view_action(action)
    if parsed is None or callback.message is None:
        await _view_notice(callback, "Кнопка устарела.")
        return
    if role != "student":
        await _view_notice(callback, "Карточки пока доступны для учебных групп.")
        return
    view, lesson_date = parsed
    await _view_notice(callback)
    try:
        lessons = await repository_async(repository).lessons_for(
            role="student", target=target, lesson_date=lesson_date,
        )
        as_card = view == "card" and bool(lessons)
        if as_card:
            try:
                content = await _rich_card(lessons, target, lesson_date, repository, settings)
            except (CardNotSupported, CardBusy, CardTimeout):
                as_card = False
                await _view_notice(callback, "Карточка сейчас недоступна. Показываю полное расписание текстом.")
            except Exception as error:
                logger.warning("Schedule view card unavailable (%s)", type(error).__name__)
                as_card = False
                await _view_notice(callback, "Карточка временно недоступна. Показываю расписание текстом.")
        if not as_card:
            text = await _schedule_text(lessons, target, lesson_date, repository, settings)
            content = _rich_text(text)
        try:
            await callback.message.edit_text(
                rich_message=content, parse_mode=None,
                reply_markup=schedule_view_keyboard(owner_id, lesson_date, as_card=as_card),
            )
        except TelegramBadRequest as error:
            if "message is not modified" not in str(error).casefold():
                raise
    except Exception as error:
        logger.warning("Schedule view could not be changed (%s)", type(error).__name__)
        await _view_notice(callback, "Не удалось переключить вид. Попробуйте ещё раз.")


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
        caption = await _card_caption(repository, settings)
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
