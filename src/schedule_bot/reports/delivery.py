"""Bounded, leased delivery with durable Telegram outcome evidence."""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import AsyncExitStack

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from schedule_bot.async_repository import repository_async

logger = logging.getLogger(__name__)


class ReportDelivery:
    def __init__(self, bot, repository, admin_ids, *, wait_send_slot, clock, pause):
        self.bot = bot
        self.repository = repository
        self.repo = repository_async(repository)
        self.admin_ids = admin_ids
        self.wait_send_slot = wait_send_slot
        self.clock = clock
        self.pause = pause
        self.lock = asyncio.Lock()

    async def deliver_pending(self, *, limit=20):
        from schedule_bot.notifier import _destination_lock
        from schedule_bot.bot.report_admin import admin_keyboard

        delivered = 0
        async with self.lock:
            # Refetch after each send so multipart context progresses in order.
            for _ in range(limit):
                candidates = await self.repo.report_pending(now=self.clock(), limit=1)
                if not candidates:
                    break
                candidate = candidates[0]
                user_id = candidate["user_id"]
                destination = candidate["destination_id"]
                await self.wait_send_slot("user", destination)
                # The owner's lock covers sends to administrators too. /delete_me
                # takes the same lock before purging the report and its queues.
                async with AsyncExitStack() as stack:
                    await stack.enter_async_context(_destination_lock(self.repository, "report_user", user_id))
                    await stack.enter_async_context(_destination_lock(self.repository, "user", destination))
                    row = await self.repo.report_claim(candidate["id"], now=self.clock())
                    if not row:
                        continue
                    try:
                        if row["kind"] == "admin" and destination not in self.admin_ids:
                            await self.repo.report_finish_delivery(row["id"], row["lease_token"], error_category="admin_removed",
                                                                   permanent=True, now=self.clock())
                            continue
                        payload = json.loads(row["payload"])
                        # Strictly shorter than the lease. A second worker cannot
                        # reclaim while this process is still awaiting Telegram.
                        async with asyncio.timeout(60):
                            message = await self.bot.send_message(
                                destination, payload["text"], parse_mode=None,
                                reply_markup=admin_keyboard(row["report_id"]) if payload["buttons"] else None,
                            )
                    except asyncio.CancelledError:
                        await self.repo.report_finish_delivery(row["id"], row["lease_token"], error_category="interrupted",
                                                               uncertain=True, now=self.clock())
                        raise
                    except TelegramRetryAfter as error:
                        self.pause(error.retry_after)
                        await self.repo.report_finish_delivery(row["id"], row["lease_token"], error_category=type(error).__name__,
                                                               retry_after=error.retry_after, now=self.clock())
                        # Stop the batch so a global Telegram flood limit isn't
                        # retriggered immediately by another report destination.
                        break
                    except (TelegramForbiddenError, TelegramBadRequest) as error:
                        await self.repo.report_finish_delivery(row["id"], row["lease_token"], error_category=type(error).__name__,
                                                               permanent=True, now=self.clock())
                    except Exception as error:
                        # Exception messages may contain complaint text or URLs.
                        await self.repo.report_finish_delivery(row["id"], row["lease_token"], error_category=type(error).__name__,
                                                               uncertain=True, now=self.clock())
                        logger.warning("Report delivery uncertain (%s)", type(error).__name__)
                    else:
                        await self.repo.report_finish_delivery(row["id"], row["lease_token"],
                                                               message_id=getattr(message, "message_id", None), now=self.clock())
                        delivered += 1
        return delivered
