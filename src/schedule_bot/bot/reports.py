"""Private, persistent report dialogs. No schedule reads or FSM draft storage."""
from __future__ import annotations

import json
import logging
import re

from aiogram import BaseMiddleware, F, Router
from aiogram.enums import ChatType
from aiogram.filters import BaseFilter, Command, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from schedule_bot.async_repository import repository_async
from schedule_bot.reports.models import (
    CATEGORIES, CHOICE_PAGE_SIZE, STATUSES, ReportError, context_heading, lesson_choices,
    message_chunks, report_summary,
)
from schedule_bot.reports.service import ReportService

logger = logging.getLogger(__name__)
router = Router(name="reports")
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)


class ReportPrivacyMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        try:
            return await handler(event, data)
        except Exception as error:
            # Even a failing acknowledgement may embed a Telegram method with
            # the complaint in its exception. Never pass it to aiogram logging.
            logger.warning("Report Telegram operation failed (%s)", type(error).__name__)


router.message.middleware(ReportPrivacyMiddleware())
router.callback_query.middleware(ReportPrivacyMiddleware())


def service(repository, settings):
    return ReportService(repository, getattr(settings, "admin_telegram_ids", ()))


def button(label, data):
    if len(data.encode()) > 64:
        raise ValueError("Report callback too long")
    return [InlineKeyboardButton(text=label[:120], callback_data=data)]


async def answer_text(message, text, markup=None):
    chunks = message_chunks(text)
    for index, chunk in enumerate(chunks):
        await message.answer(chunk, parse_mode=None, reply_markup=markup if index == len(chunks) - 1 else None)


async def show_draft(message, draft):
    prefix = f'rpd:{draft["id"]}:{draft["revision"]}:'
    snapshot = json.loads(draft["snapshot_json"])
    rows = []
    stage = draft["stage"]
    if stage == "lesson":
        text = f"⚠️ Сообщить об ошибке\n\n{context_heading(snapshot)}\n\nВ каком занятии обнаружена ошибка?"
        choices = lesson_choices(snapshot)
        page = draft["lesson_page"]
        start = page * CHOICE_PAGE_SIZE
        rows = [button(f"{index + start + 1}. {label}", prefix + "lesson:" + key)
                for index, (key, label) in enumerate(choices[start:start + CHOICE_PAGE_SIZE])]
        if page:
            rows.append(button("← Предыдущие занятия", prefix + f"page:{page - 1}"))
        if start + CHOICE_PAGE_SIZE < len(choices):
            rows.append(button("Следующие занятия →", prefix + f"page:{page + 1}"))
        rows.append(button("Проблема со всем расписанием", prefix + "lesson:day"))
    elif stage == "category":
        text = "Что указано неправильно?"
        rows = [button(label, prefix + "category:" + key) for key, label in CATEGORIES.items()]
    elif stage == "description":
        text = ('Опишите, что именно указано неверно и как должно быть.\n\n'
                'Например: «У первой пары указана аудитория 45, но занятие проходит в 47».\n\n'
                'От 5 до 1000 символов, обычный текст без вложений.')
    else:
        summary = dict(draft, lesson_key=draft["selected_lesson_key"])
        text = "Проверьте обращение\n\n" + report_summary(summary)
        rows = [button("Отправить", prefix + "send"), button("Изменить", prefix + "edit")]
    if stage != "lesson":
        rows.append(button("Назад", prefix + "back"))
    rows.append(button("Отмена", prefix + "cancel"))
    await answer_text(message, text, InlineKeyboardMarkup(inline_keyboard=rows))


async def failure(message, error):
    if isinstance(error, ReportError):
        text = str(error)
    else:
        logger.warning("Report operation failed (%s)", type(error).__name__)
        text = "Не удалось сохранить действие. Попробуйте ещё раз."
    await answer_text(message, text)


class ReportNavigationMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        text = event.text or ""
        if (event.chat.type == ChatType.PRIVATE and event.from_user
                and (re.match(r"^/(?:start|menu)(?:@\w+)?(?:\s|$)", text)
                     or text == "Сменить профиль")):
            if not re.match(r"^/start(?:@\w+)?\s+report_", text):
                try:
                    await repository_async(data["repository"]).report_cancel(event.from_user.id)
                except Exception as error:
                    logger.warning("Report navigation cleanup unavailable (%s)", type(error).__name__)
                if data.get("state"):
                    await data["state"].clear()
                    data["raw_state"] = None
        return await handler(event, data)


@router.message(CommandStart(deep_link=True), F.text.regexp(r"^/start(?:@\w+)?\s+report_"))
async def open_report_link(message, repository, settings, state):
    if not message.from_user:
        return
    try:
        token = (message.text or "").split(maxsplit=1)[1].removeprefix("report_")
        draft = await service(repository, settings).open(token, message.from_user.id, group_only=True)
        await state.clear()
        await show_draft(message, draft)
    except Exception as error:
        await failure(message, error)


@router.callback_query(F.data.startswith("rpc:"))
async def open_report_callback(callback, repository, settings, state):
    try:
        draft = await service(repository, settings).open(
            callback.data[4:], callback.from_user.id, source_chat_id=callback.message.chat.id,
            source_message_id=callback.message.message_id,
        )
        await state.clear()
        await callback.answer()
        await show_draft(callback.message, draft)
    except Exception as error:
        await failure(callback.message, error)
        await callback.answer()


@router.callback_query(F.data.startswith("rpd:"))
async def report_action(callback, repository, settings, notifier=None):
    try:
        match = re.fullmatch(r"rpd:([A-Za-z0-9_-]{16}):(\d{1,9}):(.{1,35})", callback.data or "")
        if not match:
            raise ReportError("Кнопка недействительна.")
        identifier, revision, action = match.groups()
        reports = service(repository, settings)
        await callback.answer()
        if action == "send":
            saved = await reports.submit(identifier, callback.from_user.id, int(revision))
            if notifier:
                notifier.wake_reports()
            await answer_text(callback.message,
                              f'✅ Обращение #{saved["id"]} зарегистрировано.\n\nСтатус: {STATUSES[saved["status"]]}\n\n'
                              'Сообщение сохранено. Администратор получит уведомление.\nОтвет придёт сюда, в личный чат.')
        else:
            draft = await reports.update(identifier, callback.from_user.id, int(revision), action)
            if draft:
                await show_draft(callback.message, draft)
            else:
                await answer_text(callback.message, "Оформление обращения отменено.")
    except Exception as error:
        await failure(callback.message, error)


class ReportInput(BaseFilter):
    async def __call__(self, message, repository, settings):
        if not message.from_user or (message.text or "").startswith("/"):
            return False
        if message.text in {"ща", "Ща", "Сменить профиль"} or (message.text or "").startswith("Уведомления:"):
            return False
        try:
            draft = await repository_async(repository).report_active_draft(message.from_user.id)
            return {"report_draft": draft} if draft else False
        except Exception as error:
            logger.warning("Report draft lookup failed (%s)", type(error).__name__)
            return False


@router.message(Command("cancel"))
async def cancel_report(message, repository, state):
    if message.from_user:
        await repository_async(repository).report_cancel(message.from_user.id)
    await state.clear()
    await answer_text(message, "Оформление обращения или ответа отменено.")


@router.message(ReportInput())
async def report_description(message, repository, settings, report_draft):
    try:
        service(repository, settings).available()
        if report_draft["stage"] != "description":
            await show_draft(message, report_draft)
            return
        if not message.text:
            raise ReportError("Принимается только обычный текст. Вложения не поддерживаются.")
        draft = await service(repository, settings).describe(message.from_user.id, message.text)
        await show_draft(message, draft)
    except Exception as error:
        await failure(message, error)
