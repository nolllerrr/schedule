import socket

from schedule_bot.bot.session import create_telegram_session


def test_telegram_session_forces_ipv4() -> None:
    session = create_telegram_session(
        proxy_url=None, force_ipv4=True, request_retries=5
    )
    try:
        assert session._connector_init["family"] == socket.AF_INET
        assert session._connector_init["force_close"] is True
        assert len(session.middleware) == 1
    finally:
        # No aiohttp ClientSession is created until the first request.
        assert session._session is None
