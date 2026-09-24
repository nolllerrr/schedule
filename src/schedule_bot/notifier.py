from __future__ import annotations

import asyncio
import logging
from typing import Literal, cast

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from schedule_bot.analytics import UsageAnalytics
from schedule_bot.change_policy import should_notify
from schedule_bot.domain import ImportResult, ScheduleChange
from schedule_bot.message_utils import split_message as _split_message
from schedule_bot.presentation import format_changes, format_schedule
from schedule_bot.repository import ScheduleRepository
from schedule_bot.services import changes_for_profile


logger = logging.getLogger(__name__)


class ScheduleNotifier:
    def __init__(
        self,
        bot: Bot,
        repository: ScheduleRepository,
        admin_ids: tuple[int, ...],
        *,
        analytics: UsageAnalytics | None = None,
    ) -> None:
        self.bot = bot
        self.repository = repository
        self.admin_ids = admin_ids
        self.analytics = analytics

    async def notify_import(self, result: ImportResult) -> None:
        if not result.skipped and result.changes and result.import_id is not None:
            self._enqueue_import(result)
        await self.deliver_pending()

    def _enqueue_import(self, result: ImportResult) -> None:
        if result.import_id is None:
            return
        notifiable_changes = tuple(
            change for change in result.changes if should_notify(change)
        )
        if not notifiable_changes:
            return
        deliveries: list[dict[str, object]] = []
        for profile in self.repository.subscribers():
            role = cast(Literal["student", "teacher"], profile["role"])
            target = str(profile["target"])
            changes = changes_for_profile(
                notifiable_changes, role=role, target=target
            )
            if not changes:
                continue
            text, _ = self._notification_text(changes, role=role, target=target)
            for index, chunk in enumerate(_split_message(text)):
                deliveries.append(
                    {
                        "import_id": result.import_id,
                        "destination_id": int(profile["user_id"]),
                        "destination_kind": "user",
                        "chat_type": "private",
                        "chunk_index": index,
                        "text": chunk,
                    }
                )

        for profile in self.repository.chat_subscribers():
            target = str(profile["target"])
            changes = changes_for_profile(
                notifiable_changes,
                role="student",
                target=target,
            )
            if not changes:
                continue
            text, _ = self._notification_text(
                changes,
                role="student",
                target=target,
            )
            chat_id = int(profile["chat_id"])
            for index, chunk in enumerate(_split_message(text)):
                deliveries.append(
                    {
                        "import_id": result.import_id,
                        "destination_id": chat_id,
                        "destination_kind": "chat",
                        "chat_type": str(profile["chat_type"]),
                        "chunk_index": index,
                        "text": chunk,
                    }
                )
        self.repository.enqueue_notifications(deliveries)

    def _notification_text(
        self,
        changes: tuple[ScheduleChange, ...],
        *,
        role: Literal["student", "teacher"],
        target: str,
    ) -> tuple[str, bool]:
        dates = sorted({change.lesson_date for change in changes})
        only_additions = all(change.kind == "added" for change in changes)
        if not only_additions:
            return format_changes(changes), False
        parts = ["<b>Опубликовано новое расписание</b>"]
        for lesson_date in dates:
            lessons = self.repository.lessons_for(
                role=role,
                target=target,
                lesson_date=lesson_date,
            )
            parts.append(
                format_schedule(
                    lessons,
                    target=target,
                    lesson_date=lesson_date,
                    role=role,
                )
            )
        return "\n\n".join(parts), True

    async def notify_admins_error(
        self,
        error: Exception,
        *,
        context: str = "автоматического обновления расписания",
    ) -> None:
        text = (
            f"Ошибка {context}: "
            f"{type(error).__name__}: {error}"
        )
        for admin_id in self.admin_ids:
            try:
                await self.bot.send_message(admin_id, text)
            except Exception:
                logger.exception("Could not notify administrator %s", admin_id)

    async def deliver_pending(self, *, limit: int = 100) -> None:
        blocked_destinations: set[tuple[str, int]] = set()
        for delivery in self.repository.pending_notifications(limit=limit):
            destination_id = int(delivery["destination_id"])
            destination_kind = cast(
                Literal["user", "chat"], delivery["destination_kind"]
            )
            destination_key = (destination_kind, destination_id)
            if destination_key in blocked_destinations:
                continue
            delivered = await self._deliver(delivery)
            if not delivered:
                blocked_destinations.add(destination_key)

    async def _deliver(self, delivery: dict[str, object]) -> bool:
        delivery_id = int(delivery["id"])
        destination_id = int(delivery["destination_id"])
        destination_kind = cast(
            Literal["user", "chat"], delivery["destination_kind"]
        )
        chat_type = str(delivery["chat_type"])
        error_text = "Unknown delivery error"
        permanent = False
        for attempt in range(2):
            try:
                sent = await self.bot.send_message(
                    destination_id, str(delivery["text"])
                )
                self.repository.mark_notification_sent(delivery_id)
                if self.analytics:
                    await self.analytics.track(
                        "notification_sent",
                        actor_id=destination_id,
                        actor_kind=destination_kind,
                        chat_type=chat_type,
                    )
                return True
            except TelegramRetryAfter as error:
                error_text = f"{type(error).__name__}: {error}"
                if attempt == 0:
                    await asyncio.sleep(error.retry_after)
                    continue
                logger.warning(
                    "Telegram rate limit for destination %s", destination_id
                )
            except TelegramForbiddenError as error:
                error_text = f"{type(error).__name__}: {error}"
                permanent = True
                logger.info(
                    "Bot is no longer allowed to message destination %s",
                    destination_id,
                )
                if destination_kind == "chat":
                    self.repository.deactivate_chat(destination_id)
                break
            except TelegramBadRequest as error:
                error_text = f"{type(error).__name__}: {error}"
                permanent = True
                logger.exception(
                    "Telegram rejected notification for %s", destination_id
                )
                break
            except Exception as error:
                error_text = f"{type(error).__name__}: {error}"
                logger.exception("Could not notify destination %s", destination_id)
                break
        self.repository.mark_notification_failed(
            delivery_id,
            error_text,
            permanent=permanent,
        )
        if self.analytics:
            await self.analytics.track(
                "notification_failed",
                actor_id=destination_id,
                actor_kind=destination_kind,
                chat_type=chat_type,
            )
        return False
