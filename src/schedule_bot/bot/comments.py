"""Administrator-only group comments requested from the bot's private chat."""
from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime
from weakref import WeakKeyDictionary
from zoneinfo import ZoneInfo

from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from schedule_bot.async_repository import repository_async
from schedule_bot.bot.menus import callback_data
from schedule_bot.config import Settings
from schedule_bot.notifier import _destination_lock
from schedule_bot.presentation import format_schedule
from schedule_bot.reactions import _strip_html, comment_chats_for_group
from schedule_bot.repository import ScheduleRepository
from schedule_bot.schedule_selection import current_schedule
from schedule_grok import generate_grok_reaction


logger = logging.getLogger(__name__)
_generation_locks: WeakKeyDictionary = WeakKeyDictionary()


def can_request_comment(
    settings: Settings | None, user_id: int, profile: dict[str, object],
) -> bool:
    return (
        user_id in getattr(settings, "admin_telegram_ids", ())
        and profile["role"] == "student"
    )


async def generate_group_comment(
    callback: CallbackQuery,
    repository: ScheduleRepository,
    settings: Settings,
    profile: dict[str, object],
    *,
    action: str = "new_comment",
) -> None:
    if callback.message is None or callback.message.chat.type != "private":
        await callback.answer("Кнопка доступна только в личном чате.", show_alert=True)
        return
    if callback.from_user.id not in settings.admin_telegram_ids:
        await callback.answer("Кнопка доступна только администратору бота.", show_alert=True)
        return
    if profile["role"] != "student":
        await callback.answer("Сначала выберите учебную группу через /start.", show_alert=True)
        return
    if not settings.grok_reactions_enabled:
        await callback.answer("Генерация комментариев выключена в настройках бота.", show_alert=True)
        return

    lock = _generation_locks.setdefault(asyncio.get_running_loop(), asyncio.Lock())
    if lock.locked():
        await callback.answer("Комментарий уже генерируется. Дождитесь результата.", show_alert=True)
        return
    async with lock:
        target = str(profile["target"])
        chats = await repository_async(repository).call(
            comment_chats_for_group, repository, settings, target,
        )
        if not chats:
            await callback.answer(
                "Нет доступного чата для выбранной группы. Проверьте /setup, "
                "уведомления и GROK_REACTION_CHAT_IDS.", show_alert=True,
            )
            return
        if action == "new_comment" and len(chats) > 1:
            await callback.answer()
            await callback.message.answer(
                f"Куда отправить новый комментарий для {html.escape(target)}?",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(
                        text=str(chat["chat_title"] or chat["chat_id"]),
                        callback_data=callback_data(
                            "pnav", callback.from_user.id, f"new_comment_{chat['chat_id']}",
                        ),
                    )]
                    for chat in chats
                ]),
            )
            return
        if action == "new_comment":
            chat = chats[0]
        else:
            chat = next(
                (chat for chat in chats if action == f"new_comment_{chat['chat_id']}"), None,
            )
            if chat is None:
                await callback.answer("Чат больше недоступен для выбранной группы.", show_alert=True)
                return
        chat_id = int(chat["chat_id"])
        # Acknowledge immediately: CLI generation can outlast a callback's lifetime.
        await callback.answer("Генерирую новый комментарий…")
        try:
            selected = await repository_async(repository).call(
                current_schedule, repository, role="student", target=target,
                now=datetime.now(ZoneInfo(settings.timezone)),
            )
            if selected is None:
                await callback.message.answer(
                    "Для выбранной группы пока нет актуального расписания с занятиями."
                )
                return
            lesson_date, lessons = selected
            # Include semaphore wait time in the request's total generation budget.
            async with asyncio.timeout(settings.grok_timeout_seconds):
                reaction = await generate_grok_reaction(
                    _strip_html(format_schedule(lessons, target, lesson_date, "student")),
                    enabled=True,
                    cli_path=settings.grok_cli_path,
                    timeout=settings.grok_timeout_seconds,
                )
        except Exception as error:
            logger.warning("Manual Grok comment unavailable (%s)", type(error).__name__)
            reaction = None
        if not reaction:
            await callback.message.answer("Не удалось сгенерировать комментарий. Попробуйте ещё раз.")
            return
        try:
            async with _destination_lock(repository, "chat", chat_id):
                # Chat setup/notification changes share this lock with final delivery.
                current_chats = await repository_async(repository).call(
                    comment_chats_for_group, repository, settings, target,
                )
                current_chat = next(
                    (chat for chat in current_chats if chat["chat_id"] == chat_id), None,
                )
                if current_chat is None:
                    await callback.message.answer(
                        "Комментарий не отправлен: чат или его настройки изменились."
                    )
                    return
                await callback.bot.send_message(chat_id, reaction, parse_mode=None)
        except Exception as error:
            logger.warning("Manual Grok comment delivery failed (%s)", type(error).__name__)
            await callback.message.answer(
                "Не удалось отправить комментарий в групповой чат. "
                "Проверьте доступ бота к чату."
            )
            return
        title = str(current_chat["chat_title"] or chat_id)
        await callback.message.answer(
            f"Комментарий отправлен в «{html.escape(title)}» "
            f"({html.escape(target)} · {lesson_date:%d.%m.%Y})."
        )
