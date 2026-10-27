import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import SendMessage
from test_delivery_audit import attempts, queued, worker

from schedule_bot.bot import session

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("text_schedule_media")]


@pytest.mark.parametrize("outcome", ["success", "exhausted", "cancelled"])
@pytest.mark.parametrize("admin", [False, True])
async def test_retry_middleware_and_durable_delivery(tmp_path, monkeypatch, outcome, admin):
    repository = queued(tmp_path, admin=admin)
    delays = []
    sleeping = asyncio.Event()
    close = AsyncMock()
    bot = SimpleNamespace(session=SimpleNamespace(close=close))
    request_count = 0

    async def request(_bot, method):
        nonlocal request_count
        request_count += 1
        if outcome == "success" and request_count == 2:
            return SimpleNamespace(message_id=456)
        raise TelegramNetworkError(method=method, message="synthetic network error")

    async def sleep(delay):
        delays.append(delay)
        sleeping.set()
        if outcome == "cancelled":
            await asyncio.Event().wait()

    monkeypatch.setattr(session, "asyncio", SimpleNamespace(sleep=sleep))
    middleware = session.TelegramNetworkRetry(3)

    async def send(chat_id, text, **kwargs):
        return await middleware(request, bot, SendMessage(chat_id=chat_id, text=text))

    notifier = worker(repository, send)
    task = asyncio.create_task(notifier.deliver_pending())
    if outcome == "cancelled":
        await asyncio.wait_for(sleeping.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert await task == (1 if outcome == "success" else 0)

    rows = attempts(repository)
    assert len(rows) == 1
    if outcome == "success":
        assert (request_count, delays, close.await_count) == (2, [1], 1)
        assert (rows[0]["outcome"], rows[0]["telegram_message_id"]) == ("sent", 456)
        assert repository.notification_outbox_counts() == ({} if admin else {"sent": 1})
    elif outcome == "exhausted":
        assert (request_count, delays, close.await_count) == (3, [1, 2], 2)
        assert rows[0]["outcome"] == "uncertain"
        assert rows[0]["error_category"] == "TelegramNetworkError"
        if not admin:
            assert repository.notification_outbox_counts() == {"pending": 1}
    else:
        assert (request_count, delays, close.await_count) == (1, [1], 1)
        assert (rows[0]["outcome"], rows[0]["error_category"]) == ("uncertain", "interrupted")
        if not admin:
            assert repository.notification_outbox_counts() == {"pending": 1}
