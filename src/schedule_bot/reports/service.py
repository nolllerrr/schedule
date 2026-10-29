from __future__ import annotations

import logging
import re
import asyncio
from datetime import date
from types import SimpleNamespace

from aiogram.types import InlineKeyboardButton

from schedule_bot.async_repository import repository_async
from schedule_bot.reports.models import ReportError, make_snapshot

logger = logging.getLogger(__name__)


class ReportService:
    def __init__(self, repository, admin_ids):
        self.repository = repository_async(repository)
        self.admin_ids = tuple(value for value in admin_ids if type(value) is int and value > 0)

    def available(self):
        if not self.admin_ids:
            raise ReportError("Приём обращений сейчас недоступен.")

    async def open(self, token, user_id, **kwargs):
        self.available()
        if not re.fullmatch(r"[A-Za-z0-9_-]{32}", token):
            raise ReportError("Ссылка недействительна. Откройте свежее расписание.")
        return await self.repository.report_open_draft(token, user_id, **kwargs)

    async def update(self, identifier, user_id, revision, action):
        self.available()
        return await self.repository.report_update_draft(identifier, user_id, revision, action)

    async def describe(self, user_id, text):
        self.available()
        return await self.repository.report_describe(user_id, text)

    async def submit(self, identifier, user_id, revision):
        self.available()
        return await self.repository.report_submit(identifier, user_id, revision, self.admin_ids)


async def report_button(repository, settings, bot, *, owner_id, chat_id, group, role, target, day, lessons):
    """An optional extension: failure must never suppress schedule delivery."""
    admins = tuple(value for value in getattr(settings, "admin_telegram_ids", ()) if type(value) is int and value > 0)
    if not admins:
        return None
    try:
        username = None
        if group:
            username = (await asyncio.wait_for(bot.me(), timeout=3)).username
            if not username or not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
                return None
        token = await repository_async(repository).report_create_context(
            source_kind="group" if group else "private", owner_user_id=None if group else owner_id,
            source_chat_id=chat_id, snapshot=make_snapshot(role, target, day, lessons),
        )
        if group:
            return InlineKeyboardButton(text="⚠️ Сообщить об ошибке", url=f"https://t.me/{username}?start=report_{token}")
        return InlineKeyboardButton(text="⚠️ Сообщить об ошибке", callback_data=f"rpc:{token}")
    except Exception as error:
        logger.warning("Report context unavailable (%s)", type(error).__name__)
        return None


async def bind_report_message(repository, control, message):
    if control is None or not isinstance(getattr(message, "message_id", None), int):
        return
    try:
        token = control.callback_data[4:] if control.callback_data else control.url.rsplit("report_", 1)[-1]
        await repository_async(repository).report_bind_context(token, message.chat.id, message.message_id)
    except Exception as error:
        logger.warning("Report source binding unavailable (%s)", type(error).__name__)


async def notification_report_button(repository, admin_ids, bot, delivery):
    if not admin_ids:
        return None
    try:
        from schedule_bot.schedule_media import snapshot_lessons
        payload = delivery["card_payload"]
        if "lessons" not in payload:
            if not payload.get("last", True):
                return None
            payload = await repository_async(repository).report_notification_snapshot(delivery["id"])
        if not payload:
            return None
        destination = int(delivery["destination_id"])
        return await report_button(
            repository, SimpleNamespace(admin_telegram_ids=admin_ids), bot,
            owner_id=destination, chat_id=destination, group=delivery["destination_kind"] == "chat",
            role=payload["role"], target=payload["target"], day=date.fromisoformat(payload["date"]),
            lessons=snapshot_lessons(payload),
        )
    except Exception as error:
        logger.warning("Report notification context unavailable (%s)", type(error).__name__)
        return None
