from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import TelegramMethod


logger = logging.getLogger(__name__)


class TelegramNetworkRetry:
    def __init__(self, attempts: int) -> None:
        self.attempts = max(1, attempts)

    async def __call__(
        self,
        make_request: Any,
        bot: Bot,
        method: TelegramMethod[Any],
    ) -> Any:
        for attempt in range(1, self.attempts + 1):
            try:
                return await make_request(bot, method)
            except TelegramNetworkError:
                if attempt == self.attempts:
                    raise
                delay = min(2 ** (attempt - 1), 8)
                logger.warning(
                    "Telegram request %s failed; retry %s/%s in %s seconds",
                    type(method).__name__,
                    attempt + 1,
                    self.attempts,
                    delay,
                )
                await bot.session.close()
                await asyncio.sleep(delay)



def create_telegram_session(
    *, proxy_url: str | None, force_ipv4: bool, request_retries: int = 5
) -> AiohttpSession:
    session = AiohttpSession(proxy=proxy_url)
    session._connector_init["force_close"] = True
    if force_ipv4:
        # Aiogram does not expose aiohttp TCPConnector options publicly.
        # The Telegram IPv6 route is unreliable on the target Windows host,
        # while IPv4 is reachable through the same VPN.
        session._connector_init["family"] = socket.AF_INET
    session.middleware(TelegramNetworkRetry(request_retries))
    return session
