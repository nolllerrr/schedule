from __future__ import annotations

import asyncio
import logging
from datetime import date
from time import monotonic
from typing import Iterator, Literal, cast

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
        broadcast_interval: float = 0.05,
        private_interval: float = 1.1,
        group_interval: float = 3.1,
    ) -> None:
        self.bot = bot
        self.repository = repository
        self.admin_ids = admin_ids
        self.analytics = analytics
        self.broadcast_interval = broadcast_interval
        self.private_interval = private_interval
        self.group_interval = group_interval
        self._delivery_lock = asyncio.Lock()
        self._pending_event = asyncio.Event()
        self._worker_running = False
        self._next_broadcast_at = 0.0
        self._next_destination_at: dict[tuple[str, int], float] = {}
        self._pause_until = 0.0

    async def notify_import(self, result: ImportResult) -> None:
        if not result.skipped and result.changes and result.import_id is not None:
            await asyncio.to_thread(self._enqueue_import, result)
        self._pending_event.set()
        if not self._worker_running:
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
            for index, chunk in enumerate(
                self._notification_chunks(
                    changes, role=role, target=target, new_dates=result.new_dates
                )
            ):
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
            chat_id = int(profile["chat_id"])
            for index, chunk in enumerate(
                self._notification_chunks(
                    changes, role="student", target=target, new_dates=result.new_dates
                )
            ):
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
        deliveries.sort(key=lambda item: int(item["chunk_index"]))
        self.repository.enqueue_notifications(deliveries)

    def _notification_chunks(
        self,
        changes: tuple[ScheduleChange, ...],
        *,
        role: Literal["student", "teacher"],
        target: str,
        new_dates: tuple[date, ...],
    ) -> Iterator[str]:
        for lesson_date in sorted({change.lesson_date for change in changes}):
            day_changes = tuple(
                change for change in changes if change.lesson_date == lesson_date
            )
            text, _ = self._notification_text(
                day_changes, role=role, target=target,
                new_schedule=lesson_date in new_dates,
            )
            yield from _split_message(text)

    def _notification_text(
        self,
        changes: tuple[ScheduleChange, ...],
        *,
        role: Literal["student", "teacher"],
        target: str,
        new_schedule: bool,
    ) -> tuple[str, bool]:
        dates = sorted({change.lesson_date for change in changes})
        if not new_schedule:
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

    async def run_delivery_worker(self, *, idle_seconds: float = 5.0) -> None:
        self._worker_running = True
        logger.info("Notification delivery worker started")
        try:
            while True:
                self._pending_event.clear()
                try:
                    sent_count = await self.deliver_pending()
                except Exception:
                    logger.exception("Notification worker failed; retrying")
                    sent_count = 0
                if sent_count:
                    logger.info("Notification worker delivered %s messages", sent_count)
                if sent_count == 100:
                    await asyncio.sleep(0)
                    continue
                try:
                    await asyncio.wait_for(
                        self._pending_event.wait(), timeout=idle_seconds
                    )
                except TimeoutError:
                    pass
        finally:
            self._worker_running = False

    async def deliver_pending(self, *, limit: int = 100) -> int:
        async with self._delivery_lock:
            sent_count = 0
            blocked_destinations: set[tuple[str, int]] = set()
            attempted_ids: set[int] = set()
            while sent_count < limit:
                batch = self.repository.pending_notifications(limit=limit)
                candidates = [
                    delivery
                    for delivery in batch
                    if int(delivery["id"]) not in attempted_ids
                    and (
                        str(delivery["destination_kind"]),
                        int(delivery["destination_id"]),
                    ) not in blocked_destinations
                ]
                if not candidates:
                    break
                for delivery in candidates:
                    if sent_count >= limit:
                        break
                    attempted_ids.add(int(delivery["id"]))
                    destination_id = int(delivery["destination_id"])
                    destination_kind = cast(
                        Literal["user", "chat"], delivery["destination_kind"]
                    )
                    delivered = await self._deliver(delivery)
                    if delivered:
                        sent_count += 1
                    else:
                        blocked_destinations.add(
                            (destination_kind, destination_id)
                        )
            return sent_count

    async def _wait_send_slot(
        self, destination_kind: str, destination_id: int
    ) -> None:
        destination_key = (destination_kind, destination_id)
        ready_at = max(
            self._next_broadcast_at,
            self._next_destination_at.get(destination_key, 0.0),
            self._pause_until,
        )
        delay = ready_at - monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        now = monotonic()
        self._next_broadcast_at = now + self.broadcast_interval
        destination_interval = (
            self.private_interval
            if destination_kind == "user"
            else self.group_interval
        )
        self._next_destination_at[destination_key] = now + destination_interval

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
                await self._wait_send_slot(destination_kind, destination_id)
                await self.bot.send_message(
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
                logger.warning(
                    "Telegram rate limit: waiting %s seconds", error.retry_after
                )
                self._pause_until = max(
                    self._pause_until, monotonic() + error.retry_after
                )
                if attempt == 0:
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
