import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import SendMessage

from schedule_bot.bot.session import TelegramNetworkRetry, create_telegram_session


def test_telegram_session_forces_ipv4() -> None:
    session = create_telegram_session(
        proxy_url=None, force_ipv4=True, request_retries=5
    )
    try:
        assert session._connector_init["family"] == socket.AF_INET
        assert "force_close" not in session._connector_init
        assert len(session.middleware) == 1
    finally:
        # No aiohttp ClientSession is created until the first request.
        assert session._session is None


def network_error(method: SendMessage) -> TelegramNetworkError:
    return TelegramNetworkError(method=method, message="connection lost")


@pytest.mark.asyncio
async def test_retry_returns_first_success_without_closing_or_sleeping(monkeypatch) -> None:
    sleep = AsyncMock()
    monkeypatch.setattr("schedule_bot.bot.session.asyncio.sleep", sleep)
    bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock()))
    method = SendMessage(chat_id=123, text="hello")
    request = AsyncMock(return_value="result")

    assert await TelegramNetworkRetry(5)(request, bot, method) == "result"
    request.assert_awaited_once_with(bot, method)
    bot.session.close.assert_not_awaited()
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_closes_before_delay_and_retries_same_request(monkeypatch) -> None:
    events = []
    method = SendMessage(chat_id=123, text="hello")
    error = network_error(method)

    async def request(bot, requested_method):
        events.append(("request", bot, requested_method))
        if len([event for event in events if event[0] == "request"]) == 1:
            raise error
        return "result"

    async def close():
        events.append(("close",))

    async def sleep(delay):
        events.append(("sleep", delay))

    monkeypatch.setattr("schedule_bot.bot.session.asyncio.sleep", sleep)
    bot = SimpleNamespace(session=SimpleNamespace(close=close))
    assert await TelegramNetworkRetry(3)(request, bot, method) == "result"
    assert events == [
        ("request", bot, method), ("close",), ("sleep", 1),
        ("request", bot, method),
    ]


@pytest.mark.asyncio
async def test_retry_exhaustion_propagates_last_error_and_caps_delays(monkeypatch) -> None:
    sleep = AsyncMock()
    monkeypatch.setattr("schedule_bot.bot.session.asyncio.sleep", sleep)
    bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock()))
    method = SendMessage(chat_id=123, text="hello")
    errors = [network_error(method) for _ in range(7)]
    request = AsyncMock(side_effect=errors)

    with pytest.raises(TelegramNetworkError) as raised:
        await TelegramNetworkRetry(7)(request, bot, method)
    assert raised.value is errors[-1]
    assert request.await_args_list == [call(bot, method)] * 7
    assert bot.session.close.await_count == 6
    assert sleep.await_args_list == [call(delay) for delay in (1, 2, 4, 8, 8, 8)]


@pytest.mark.asyncio
async def test_retry_does_not_retry_other_errors(monkeypatch) -> None:
    sleep = AsyncMock()
    monkeypatch.setattr("schedule_bot.bot.session.asyncio.sleep", sleep)
    bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock()))
    method = SendMessage(chat_id=123, text="hello")
    error = ValueError("bad request")
    request = AsyncMock(side_effect=error)

    with pytest.raises(ValueError) as raised:
        await TelegramNetworkRetry(5)(request, bot, method)
    assert raised.value is error
    request.assert_awaited_once_with(bot, method)
    bot.session.close.assert_not_awaited()
    sleep.assert_not_awaited()


@pytest.mark.parametrize("attempts", [1, 0, -1])
@pytest.mark.asyncio
async def test_retry_minimum_is_one_attempt(attempts, monkeypatch) -> None:
    sleep = AsyncMock()
    monkeypatch.setattr("schedule_bot.bot.session.asyncio.sleep", sleep)
    bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock()))
    method = SendMessage(chat_id=123, text="hello")
    error = network_error(method)
    request = AsyncMock(side_effect=error)

    with pytest.raises(TelegramNetworkError) as raised:
        await TelegramNetworkRetry(attempts)(request, bot, method)
    assert raised.value is error
    request.assert_awaited_once_with(bot, method)
    bot.session.close.assert_not_awaited()
    sleep.assert_not_awaited()
