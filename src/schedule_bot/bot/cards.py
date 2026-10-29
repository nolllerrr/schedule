"""On-demand card delivery; profile authorization stays in the menu handlers."""
from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from datetime import date

from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    BufferedInputFile, CallbackQuery, InputMediaPhoto, Message,
)

from schedule_bot.async_repository import repository_async
from schedule_bot.bot.menus import schedule_view_keyboard
from schedule_bot.card_renderer import CardNotSupported
from schedule_bot.card_service import CardBusy, CardTimeout, get_card_service
from schedule_bot.domain import Lesson
from schedule_bot.freshness import with_freshness
from schedule_bot.message_utils import answer_in_chunks
from schedule_bot.presentation import format_schedule
from schedule_bot.reports.service import report_button, bind_report_message


logger = logging.getLogger(__name__)


async def delete_previous_view(message: Message) -> None:
    try:
        await message.delete()
    except TelegramAPIError as error:
        logger.warning("Previous schedule view could not be deleted (%s)", type(error).__name__)


def card_action(role: str, lesson_date: date) -> str | None:
    return f"card_{lesson_date.isoformat()}" if role in {"student", "teacher"} else None


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


async def _card_photo(
    lessons: Sequence[Lesson], target: str, lesson_date: date, repository, settings, role: str = "student",
) -> InputMediaPhoto:
    png = await get_card_service().get_card(
        role=role, target=target, lesson_date=lesson_date, lessons=lessons,
    )
    caption = await _card_caption(repository, settings)
    return InputMediaPhoto(
        media=BufferedInputFile(
            png, filename=f"schedule-{lesson_date.isoformat()}.png",
        ),
        caption=caption, parse_mode=None,
    )


async def _card_caption(repository, settings) -> str | None:
    # The date is already printed inside the PNG. Only source warnings belong
    # underneath it; a fresh schedule has no caption at all.
    return (await with_freshness("", repository, settings)).strip() or None


async def _schedule_text(
    lessons: Sequence[Lesson], target: str, lesson_date: date, repository, settings, role: str = "student",
) -> str:
    return await with_freshness(
        format_schedule(lessons, target, lesson_date, role), repository, settings,
    )


async def send_current_schedule_view(
    message: Message, repository, settings, *, owner_id: int, target: str,
    lesson_date: date, lessons: Sequence[Lesson], prefix: str = "pnav", role: str = "student",
) -> None:
    """Open the current day as a card, keeping a complete text fallback."""
    report = await _report_control(message, repository, settings, owner_id, prefix, role, target, lesson_date, lessons)
    try:
        photo = await _card_photo(lessons, target, lesson_date, repository, settings, role)
        # Native photos let Telegram size the bubble to the image. Rich photo
        # blocks use the wider rich-message container instead.
        sent = await message.answer_photo(
            photo.media, caption=photo.caption, parse_mode=None,
            reply_markup=schedule_view_keyboard(owner_id, lesson_date, as_card=True, prefix=prefix, report_button=report),
        )
        await bind_report_message(repository, report, sent)
        return
    except (CardNotSupported, CardBusy, CardTimeout):
        pass
    except TelegramAPIError as error:
        logger.warning("Current schedule view delivery failed (%s)", type(error).__name__)
    except Exception as error:
        logger.warning("Current schedule card unavailable (%s)", type(error).__name__)
    text = await _schedule_text(lessons, target, lesson_date, repository, settings, role)
    sent = await answer_in_chunks(
        message, text, reply_markup=schedule_view_keyboard(owner_id, lesson_date, as_card=False, prefix=prefix, report_button=report),
    )
    if sent:
        await bind_report_message(repository, report, sent[-1])


async def _report_control(message, repository, settings, owner_id, prefix, role, target, day, lessons):
    if not getattr(settings, "admin_telegram_ids", ()):
        return None
    try:
        return await report_button(
            repository, settings, message.bot, owner_id=owner_id, chat_id=message.chat.id,
            group=prefix == "gnav", role=role, target=target, day=day, lessons=lessons,
        )
    except Exception as error:
        logger.warning("Report control unavailable (%s)", type(error).__name__)
        return None


async def _view_notice(callback: CallbackQuery, text: str = "") -> None:
    try:
        await callback.answer(text, show_alert=bool(text))
    except TelegramAPIError as error:
        logger.info("Schedule view acknowledgement failed (%s)", type(error).__name__)


async def switch_schedule_view(
    callback: CallbackQuery, repository, settings, *, owner_id: int,
    role: str, target: str, action: str, prefix: str = "pnav",
) -> None:
    """Send the requested view, then remove the previous view on success."""
    parsed = schedule_view_action(action)
    if parsed is None or callback.message is None:
        await _view_notice(callback, "Кнопка устарела.")
        return
    if role not in {"student", "teacher"}:
        await _view_notice(callback, "Профиль не поддерживает расписание.")
        return
    view, lesson_date = parsed
    await _view_notice(callback)
    try:
        lessons = await repository_async(repository).lessons_for(
            role=role, target=target, lesson_date=lesson_date,
        )
        report = await _report_control(callback.message, repository, settings, owner_id, prefix, role, target, lesson_date, lessons)
        as_card = view == "card" and bool(lessons)
        if as_card:
            try:
                photo = await _card_photo(lessons, target, lesson_date, repository, settings, role)
            except (CardNotSupported, CardBusy, CardTimeout):
                as_card = False
                await _view_notice(callback, "Карточка сейчас недоступна. Показываю полное расписание текстом.")
            except Exception as error:
                logger.warning("Schedule view card unavailable (%s)", type(error).__name__)
                as_card = False
                await _view_notice(callback, "Карточка временно недоступна. Показываю расписание текстом.")
        if as_card:
            sent = await callback.message.answer_photo(
                photo.media, caption=photo.caption, parse_mode=None,
                reply_markup=schedule_view_keyboard(owner_id, lesson_date, as_card=True, prefix=prefix, report_button=report),
            )
            await bind_report_message(repository, report, sent)
        else:
            text = await _schedule_text(lessons, target, lesson_date, repository, settings, role)
            sent = await answer_in_chunks(
                callback.message, text,
                reply_markup=schedule_view_keyboard(owner_id, lesson_date, as_card=False, prefix=prefix, report_button=report),
            )
            if sent:
                await bind_report_message(repository, report, sent[-1])
        if as_card or view == "text":
            # Keep the previous view until the replacement is fully delivered.
            # A deletion failure must not turn a successful send into an error.
            await delete_previous_view(callback.message)
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
    if role not in {"student", "teacher"}:
        await callback.answer(
            "Профиль не поддерживает расписание.",
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
            role=role, target=target, lesson_date=lesson_date,
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
        prefix = "gnav" if (callback.data or "").startswith("gnav:") else "pnav"
        report = await _report_control(callback.message, repository, settings, callback.from_user.id, prefix, role, target, lesson_date, lessons)
        sent = await callback.message.answer_photo(
            BufferedInputFile(png, filename=f"schedule-{lesson_date.isoformat()}.png"),
            caption=caption,
            parse_mode=None,
            reply_markup=schedule_view_keyboard(
                callback.from_user.id, lesson_date, as_card=True,
                prefix=prefix, report_button=report,
            ),
        )
        await bind_report_message(repository, report, sent)
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
