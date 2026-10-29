from __future__ import annotations

import re

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import BaseFilter
from aiogram.types import InlineKeyboardMarkup

from schedule_bot.async_repository import repository_async
from schedule_bot.bot.reports import answer_text, button, failure, ReportPrivacyMiddleware
from schedule_bot.reports.models import ReportError, STATUSES

router = Router(name="report_admin")
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
router.message.middleware(ReportPrivacyMiddleware())
router.callback_query.middleware(ReportPrivacyMiddleware())


def admin_keyboard(identifier):
    return InlineKeyboardMarkup(inline_keyboard=[
        button(label, f"rpa:{identifier}:{action}") for label, action in (
            ("Ответить", "reply"), ("В работу", "progress"), ("Решено", "resolved"), ("Отклонить", "rejected"),
        )
    ])


def authorized(user_id, settings):
    ids = getattr(settings, "admin_telegram_ids", ())
    if user_id not in ids:
        raise ReportError("Действие доступно только администратору.")
    return ids


async def show_admin_draft(message, draft):
    prefix = f'rpac:{draft["id"]}:{draft["revision"]}:'
    if draft["stage"] == "text":
        text = (f'Ответ на обращение #{draft["report_id"]}\n\nВведите сообщение для пользователя.' if draft["action"] == "reply"
                else f'Закрытие обращения #{draft["report_id"]}: {STATUSES[draft["action"]]}\n\nВведите итоговый комментарий.')
        rows = []
    else:
        text = f'Подтвердите сообщение по обращению #{draft["report_id"]}\n\n{draft["text"]}'
        rows = [button("Подтвердить", prefix + "send")]
    rows.append(button("Отмена", prefix + "cancel"))
    await answer_text(message, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("rpa:"))
async def admin_action(callback, repository, settings, state):
    try:
        ids = authorized(callback.from_user.id, settings)
        match = re.fullmatch(r"rpa:([1-9]\d{0,17}):(reply|progress|resolved|rejected)", callback.data or "")
        if not match:
            raise ReportError("Кнопка недействительна.")
        identifier, action = match.groups()
        await callback.answer()
        if action == "progress":
            report = await repository_async(repository).report_in_progress(int(identifier), callback.from_user.id, ids)
            await answer_text(callback.message, f'Обращение #{identifier}. Статус: {STATUSES[report["status"]]}.')
        else:
            draft = await repository_async(repository).report_admin_begin(int(identifier), callback.from_user.id, action, ids)
            await state.clear()
            await show_admin_draft(callback.message, draft)
    except Exception as error:
        await failure(callback.message, error)


@router.callback_query(F.data.startswith("rpac:"))
async def admin_confirm(callback, repository, settings, notifier=None):
    try:
        ids = authorized(callback.from_user.id, settings)
        match = re.fullmatch(r"rpac:([A-Za-z0-9_-]{16}):(\d{1,9}):(send|cancel)", callback.data or "")
        if not match:
            raise ReportError("Кнопка недействительна.")
        identifier, revision, action = match.groups()
        await callback.answer()
        if action == "cancel":
            await repository_async(repository).report_admin_cancel(identifier, callback.from_user.id, int(revision), ids)
            await answer_text(callback.message, "Оформление ответа отменено.")
        else:
            report = await repository_async(repository).report_admin_confirm(identifier, callback.from_user.id, int(revision), ids)
            if notifier:
                notifier.wake_reports()
            await answer_text(callback.message, f'Действие сохранено. Обращение #{report["id"]}: {STATUSES[report["status"]]}.\n'
                              'Сообщение поставлено в очередь, если действие ещё не выполнялось.')
    except Exception as error:
        await failure(callback.message, error)


class AdminReportInput(BaseFilter):
    async def __call__(self, message, repository, settings):
        if not message.from_user or message.from_user.id not in getattr(settings, "admin_telegram_ids", ()):
            return False
        if (message.text or "").startswith("/") or message.text in {"ща", "Ща", "Сменить профиль"}:
            return False
        try:
            draft = await repository_async(repository).report_admin_active(message.from_user.id)
        except Exception:
            return False
        return {"admin_report_draft": draft} if draft else False


@router.message(AdminReportInput())
async def admin_text(message, repository, settings, admin_report_draft):
    try:
        ids = authorized(message.from_user.id, settings)
        if admin_report_draft["stage"] != "text":
            await show_admin_draft(message, admin_report_draft)
            return
        if not message.text:
            raise ReportError("Принимается только обычный текст.")
        draft = await repository_async(repository).report_admin_text(message.from_user.id, message.text, ids)
        await show_admin_draft(message, draft)
    except Exception as error:
        await failure(message, error)
