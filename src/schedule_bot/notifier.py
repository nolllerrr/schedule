from __future__ import annotations

import asyncio
import html
import heapq
import logging
import os
from datetime import datetime, timezone
from dataclasses import dataclass
from time import monotonic
from typing import Callable, Literal, cast
from weakref import WeakKeyDictionary, WeakValueDictionary

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
from schedule_bot.security import redact_secrets, safe_exception_text


logger = logging.getLogger(__name__)
_split_message = split_message  # Compatibility for callers of the former helper.
_destination_locks: WeakKeyDictionary[
    asyncio.AbstractEventLoop, WeakValueDictionary[tuple[str, str, int], asyncio.Lock]
] = WeakKeyDictionary()


def _destination_lock(
    repository: ScheduleRepository, destination_kind: str, destination_id: int
) -> asyncio.Lock:
    locks = _destination_locks.setdefault(asyncio.get_running_loop(), WeakValueDictionary())
    key = (os.path.normcase(str(repository.database_path.resolve())), destination_kind, destination_id)
    return locks.setdefault(key, asyncio.Lock())


@dataclass(slots=True)
class _DeliveryBudget:
    remaining: int


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
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.bot = bot
        self.repository = repository
        self.admin_ids = admin_ids
        self.analytics = analytics
        self.broadcast_interval = broadcast_interval
        self.private_interval = private_interval
        self.group_interval = group_interval
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._delivery_lock = asyncio.Lock()
        self._pending_event = asyncio.Event()
        self._worker_running = False
        self._next_broadcast_at = 0.0
        self._next_destination_at: dict[tuple[str, int], float] = {}
        self._destination_expirations: list[tuple[float, tuple[str, int]]] = []
        self._pause_until = 0.0

    async def notify_import(self, result: ImportResult) -> None:
        # Deliveries already committed atomically with the schedule.
        self._pending_event.set()
        if not self._worker_running:
            await self.deliver_pending()

    async def delete_user_data(self, user_id: int, actor_key: str) -> bool:
        # Wait for an in-flight API request and its analytics, then purge both.
        # Rate-limit sleeps happen outside this lock, so they cannot delay deletion.
        async with _destination_lock(self.repository, "user", user_id):
            deleted = await asyncio.to_thread(
                self.repository.delete_user_data, user_id, actor_key
            )
            self._next_destination_at.pop(("user", user_id), None)
            self._destination_expirations = [
                entry for entry in self._destination_expirations
                if entry[1] != ("user", user_id)
            ]
            heapq.heapify(self._destination_expirations)
            return deleted

    async def notify_admins_error(
        self,
        error: Exception,
        *,
        context: str = "автоматического обновления расписания",
    ) -> None:
        text = html.escape(
            (
                f"Ошибка {redact_secrets(context)[:200]}: {type(error).__name__}: "
                f"{safe_exception_text(error)}"
            )[:650]
        )
        try:
            await asyncio.to_thread(
                self.repository.enqueue_admin_error,
                text,
                self.admin_ids,
                now=self._clock(),
            )
        except Exception:
            # A broken SQLite database must still produce a critical alert.
            # Persistence cannot help until the database becomes writable again.
            logger.exception("Could not persist critical administrator alert")
            for admin_id in self.admin_ids:
                await self._wait_send_slot("user", admin_id)
                async with _destination_lock(self.repository, "user", admin_id):
                    try:
                        await self.bot.send_message(admin_id, text)
                    except Exception:
                        logger.exception("Could not notify administrator %s", admin_id)
            return
        await self.notify_admin_pending()

    async def notify_admin_pending(self) -> None:
        self._pending_event.set()
        if not self._worker_running:
            await self.deliver_admin_pending()

    async def run_delivery_worker(self, *, idle_seconds: float = 5.0) -> None:
        self._worker_running = True
        logger.info("Notification delivery worker started")
        try:
            while True:
                self._prune_send_slots()
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
        if not isinstance(limit, int) or limit < 1:
            raise ValueError("Delivery limit must be positive")
        async with self._delivery_lock:
            budget = _DeliveryBudget(limit)
            sent_count = await self._deliver_admin_pending(limit=limit, budget=budget)
            blocked_destinations: set[tuple[str, int]] = set()
            attempted_ids: set[int] = set()
            while budget.remaining:
                batch = await asyncio.to_thread(
                    self.repository.pending_notifications,
                    limit=budget.remaining,
                    now=self._clock(),
                )
                candidates = [
                    delivery
                    for delivery in batch
                    if int(delivery["id"]) not in attempted_ids
                    and (
                        str(delivery["destination_kind"]),
                        int(delivery["destination_id"]),
                    )
                    not in blocked_destinations
                ]
                if not candidates:
                    break
                for delivery in candidates:
                    if not budget.remaining:
                        break
                    budget.remaining -= 1
                    attempted_ids.add(int(delivery["id"]))
                    destination_id = int(delivery["destination_id"])
                    destination_kind = cast(
                        Literal["user", "chat"], delivery["destination_kind"]
                    )
                    delivered = await self._deliver(delivery)
                    if delivered:
                        sent_count += 1
                    else:
                        blocked_destinations.add((destination_kind, destination_id))
            if not budget.remaining:
                self._pending_event.set()
            return sent_count

    async def deliver_admin_pending(self, *, limit: int = 100) -> int:
        """Retry persisted administrator messages, even without a new source check."""
        async with self._delivery_lock:
            return await self._deliver_admin_pending(limit=limit)

    async def _deliver_admin_pending(
        self, *, limit: int, budget: _DeliveryBudget | None = None
    ) -> int:
        if not isinstance(limit, int) or limit < 1:
            raise ValueError("Delivery limit must be positive")
        budget = budget if budget is not None else _DeliveryBudget(limit)
        sent_count = 0
        attempted_ids: set[int] = set()
        while budget.remaining:
            batch = await asyncio.to_thread(
                self.repository.pending_admin_notifications,
                limit=budget.remaining,
                now=self._clock(),
            )
            candidates = [row for row in batch if int(row["id"]) not in attempted_ids]
            if not candidates:
                break
            for delivery in candidates:
                if not budget.remaining:
                    break
                budget.remaining -= 1
                attempted_ids.add(int(delivery["id"]))
                if await self._deliver_admin(delivery):
                    sent_count += 1
        if not budget.remaining:
            self._pending_event.set()
        return sent_count

    async def _deliver_admin(self, delivery: dict[str, object]) -> bool:
        delivery_id = int(delivery["id"])
        admin_id = int(delivery["destination_id"])
        await self._wait_send_slot("user", admin_id)
        async with _destination_lock(self.repository, "user", admin_id):
            # Another notifier or a recovery may have completed/canceled it while waiting.
            if not await asyncio.to_thread(
                self.repository.admin_notification_is_sendable,
                delivery_id,
                now=self._clock(),
            ):
                return False
            permanent = False
            retry_after: float | None = None
            try:
                await self.bot.send_message(admin_id, str(delivery["text"]))
            except TelegramRetryAfter as error:
                retry_after = max(1, error.retry_after)
                self._pause_until = max(
                    self._pause_until, monotonic() + min(retry_after, 3600)
                )
                error_text = f"{type(error).__name__}: {safe_exception_text(error)}"
                logger.warning("Telegram rate limit for administrator %s", admin_id)
            except (TelegramForbiddenError, TelegramBadRequest) as error:
                permanent = True
                error_text = f"{type(error).__name__}: {safe_exception_text(error)}"
                logger.warning(
                    "Telegram rejected administrator alert for %s (%s)",
                    admin_id,
                    type(error).__name__,
                )
            except Exception as error:
                error_text = f"{type(error).__name__}: {safe_exception_text(error)}"
                logger.warning(
                    "Could not notify administrator %s (%s)",
                    admin_id,
                    type(error).__name__,
                )
            else:
                await self._acknowledge_delivery(delivery_id, admin=True)
                return True
            await asyncio.to_thread(
                self.repository.mark_admin_notification_failed,
                delivery_id,
                error_text,
                permanent=permanent,
                retry_after=retry_after,
                now=self._clock(),
            )
            return False

    async def _acknowledge_delivery(
        self, delivery_id: int, *, admin: bool = False
    ) -> None:
        # Telegram has already accepted this message. Finish the SQLite commit
        # before releasing its destination lock, including on graceful shutdown.
        operation = (
            asyncio.to_thread(
                self.repository.mark_admin_notification_sent,
                delivery_id,
                now=self._clock(),
            )
            if admin
            else asyncio.to_thread(self.repository.mark_notification_sent, delivery_id)
        )
        acknowledgment = asyncio.create_task(operation)
        try:
            await asyncio.shield(acknowledgment)
        except asyncio.CancelledError:
            await acknowledgment
            raise

    async def _wait_send_slot(self, destination_kind: str, destination_id: int) -> None:
        self._prune_send_slots()
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
            self.private_interval if destination_kind == "user" else self.group_interval
        )
        self._prune_send_slots()
        if destination_interval > 0:
            expires = now + destination_interval
            self._next_destination_at[destination_key] = expires
            heapq.heappush(self._destination_expirations, (expires, destination_key))

    def _prune_send_slots(self) -> None:
        now = monotonic()
        while self._destination_expirations and self._destination_expirations[0][0] <= now:
            expires, key = heapq.heappop(self._destination_expirations)
            if self._next_destination_at.get(key) == expires:
                self._next_destination_at.pop(key, None)

    async def _track_delivery(
        self,
        event_type: str,
        destination_id: int,
        destination_kind: Literal["user", "chat"],
        chat_type: str,
    ) -> None:
        if self.analytics is None:
            return
        try:
            await self.analytics.track(
                event_type,
                actor_id=destination_id,
                actor_kind=destination_kind,
                chat_type=chat_type,
            )
        except Exception:
            # A statistics failure must not put an already sent message back
            # into pending or interrupt durable retry bookkeeping.
            logger.exception("Could not record notification delivery statistics")

    async def _deliver(self, delivery: dict[str, object]) -> bool:
        delivery_id = int(delivery["id"])
        destination_id = int(delivery["destination_id"])
        destination_kind = cast(Literal["user", "chat"], delivery["destination_kind"])
        chat_type = str(delivery["chat_type"])
        for attempt in range(2):
            await self._wait_send_slot(destination_kind, destination_id)
            async with _destination_lock(
                self.repository, destination_kind, destination_id
            ):
                # The profile or row may have disappeared during the wait or
                # while another notifier was sending an earlier chunk.
                if not await asyncio.to_thread(
                    self.repository.notification_is_sendable,
                    delivery_id,
                    now=self._clock(),
                ):
                    return False
                error_text = ""
                permanent = False
                retry_after: float | None = None
                try:
                    await self.bot.send_message(destination_id, str(delivery["text"]))
                except TelegramRetryAfter as error:
                    error_text = f"{type(error).__name__}: {safe_exception_text(error)}"
                    retry_after = max(1, error.retry_after)
                    self._pause_until = max(
                        self._pause_until, monotonic() + retry_after
                    )
                    logger.warning(
                        "Telegram rate limit for destination %s", destination_id
                    )
                except TelegramForbiddenError as error:
                    error_text = f"{type(error).__name__}: {safe_exception_text(error)}"
                    permanent = True
                    if destination_kind == "chat":
                        await asyncio.to_thread(
                            self.repository.deactivate_chat, destination_id
                        )
                    logger.info("Bot cannot message destination %s", destination_id)
                except TelegramBadRequest as error:
                    error_text = f"{type(error).__name__}: {safe_exception_text(error)}"
                    permanent = True
                    logger.exception(
                        "Telegram rejected notification for %s", destination_id
                    )
                except Exception as error:
                    error_text = f"{type(error).__name__}: {safe_exception_text(error)}"
                    logger.exception("Could not notify destination %s", destination_id)
                else:
                    # A database failure here remains an uncertain API outcome;
                    # it must not be classified as a permanent Telegram error.
                    await self._acknowledge_delivery(delivery_id)
                    await self._track_delivery(
                        "notification_sent", destination_id, destination_kind, chat_type
                    )
                    return True

                await asyncio.to_thread(
                    self.repository.mark_notification_failed,
                    delivery_id,
                    error_text,
                    permanent=permanent,
                    retry_after=retry_after,
                    now=self._clock(),
                )
                await self._track_delivery(
                    "notification_failed", destination_id, destination_kind, chat_type
                )
                # Keep one immediate, rate-limited retry; its wait is outside
                # the destination lock so /delete_me can finish in the meantime.
                if retry_after is None or attempt == 1:
                    return False
        return False
