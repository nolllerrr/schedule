"""Use messages from a trusted bot to request an ordinary source check."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable

from aiogram import Bot, Router
from aiogram.types import Message


logger = logging.getLogger(__name__)


class ScheduleSignalListener:
    def __init__(
        self,
        username: str,
        get_chat_profile: Callable[[int], Awaitable[dict[str, object] | None]],
        update_job: Callable[[], Awaitable[None]],
        *,
        chat_id: int,
        cooldown_seconds: float = 60,
    ) -> None:
        self.username = username.lstrip("@").lower()
        self.chat_id = chat_id
        self.get_chat_profile = get_chat_profile
        self.update_job = update_job
        self.cooldown_seconds = cooldown_seconds
        self._pending = asyncio.Event()
        self._seen: OrderedDict[tuple[int, int], bytes] = OrderedDict()

    def matches(self, message: Message, bot: Bot) -> bool:
        sender = message.from_user
        return bool(
            self.username
            and message.chat.id == self.chat_id
            and message.chat.type in {"group", "supergroup"}
            and sender
            and sender.is_bot
            and sender.id != bot.id
            and (sender.username or "").lower() == self.username
            and (message.text or message.caption or message.document)
        )

    async def receive(self, message: Message, bot: Bot) -> None:
        if not self.matches(message, bot):
            return
        profile = await self.get_chat_profile(message.chat.id)
        if not profile or not profile["active"]:
            return
        key = (message.chat.id, message.message_id)
        document_id = message.document.file_unique_id if message.document else None
        fingerprint = hashlib.sha256(
            repr((message.text, message.caption, document_id)).encode("utf-8")
        ).digest()
        if self._seen.get(key) == fingerprint:
            return
        self._seen[key] = fingerprint
        self._seen.move_to_end(key)
        if len(self._seen) > 512:
            self._seen.popitem(last=False)
        self._pending.set()
        logger.info(
            "Schedule signal accepted from @%s in chat %s, message %s",
            self.username, message.chat.id, message.message_id,
        )

    async def run(self) -> None:
        next_check = 0.0
        while True:
            await self._pending.wait()
            delay = next_check - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            # Coalesce the entire burst. A signal arriving during the check
            # remains pending, including when another update holds the lock.
            self._pending.clear()
            try:
                await self.update_job()
            except Exception:
                logger.exception("Schedule check requested by a bot signal failed")
            finally:
                next_check = time.monotonic() + self.cooldown_seconds


def create_signal_router(listener: ScheduleSignalListener) -> Router:
    router = Router(name="schedule_signals")
    router.message.register(listener.receive, listener.matches)
    router.edited_message.register(listener.receive, listener.matches)
    return router
