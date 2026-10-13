from __future__ import annotations

import asyncio
import logging
from time import monotonic
from typing import Literal, cast

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from schedule_bot.analytics import UsageAnalytics
from schedule_bot.domain import ImportResult
from schedule_bot.message_utils import split_message
from schedule_bot.repository import ScheduleRepository


logger = logging.getLogger(__name__)
_split_message = split_message  # Compatibility for callers of the former helper.


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
        # Deliveries already committed atomically with the schedule.
        self._pending_event.set()
        if not self._worker_running:
            await self.deliver_pending()

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
