from __future__ import annotations

import io
import logging
import sys

import httpx
import pytest

from schedule_bot.logging_setup import (
    LOG_FORMAT,
    SecretSafeFormatter,
    configure_console_logging,
    configure_file_logging,
)
from schedule_bot.security import redact_secrets, safe_exception_text


BOT_TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk"


@pytest.mark.parametrize(
    "url, expected",
    [
        (
            "https://alice:p%40ssword@example.org/9006/?key=secret#another-secret",
            "https://[REDACTED]@example.org/9006/?[REDACTED]#[REDACTED]",
        ),
        (
            "socks5://alice:password@127.0.0.1:1080",
            "socks5://[REDACTED]@127.0.0.1:1080",
        ),
        (
            "https://user:password@[::1]:8080/9006/",
            "https://[REDACTED]@[::1]:8080/9006/",
        ),
        (
            "https://api.telegram.org/botshort-synthetic-token/sendMessage",
            "https://api.telegram.org/bot[REDACTED]/sendMessage",
        ),
        (
            "https://user:password@api.telegram.org/file/botshort-token/document.xlsx",
            "https://[REDACTED]@api.telegram.org/file/bot[REDACTED]/document.xlsx",
        ),
    ],
)
def test_redact_url_secrets_retains_source_and_path(url, expected) -> None:
    assert redact_secrets(url) == expected
    assert redact_secrets(expected) == expected


def test_redact_bare_and_encoded_bot_tokens() -> None:
    assert redact_secrets(f"token={BOT_TOKEN}") == "token=[REDACTED]"
    assert redact_secrets(BOT_TOKEN.replace(":", "%3A")) == "[REDACTED]"
    assert safe_exception_text(ValueError(f"invalid token {BOT_TOKEN}")) == (
        "invalid token [REDACTED]"
    )


def test_formatter_masks_message_args_traceback_and_chained_httpx_causes() -> None:
    stream = io.StringIO()
    logger = logging.getLogger("schedule_bot.test_safe_logging")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = logging.StreamHandler(stream)
    handler.setFormatter(SecretSafeFormatter(LOG_FORMAT))
    logger.addHandler(handler)
    try:
        try:
            try:
                raise httpx.ConnectError(
                    "Proxy failed: socks5://alice:proxy-password@proxy.test:1080"
                )
            except httpx.ConnectError as cause:
                raise RuntimeError(
                    "Could not download https://example.org/9006/?key=query-secret"
                ) from cause
        except RuntimeError:
            logger.exception("Request using token %s failed", BOT_TOKEN)
    finally:
        logger.removeHandler(handler)
        handler.close()

    output = stream.getvalue()
    for secret in ("alice", "proxy-password", "query-secret", BOT_TOKEN):
        assert secret not in output
    assert "ConnectError" in output
    assert "RuntimeError" in output
    assert "Traceback (most recent call last)" in output
    assert "The above exception was the direct cause" in output
    assert (
        "test_formatter_masks_message_args_traceback_and_chained_httpx_causes" in output
    )
    assert "example.org/9006/" in output
    assert "proxy.test:1080" in output


def test_formatter_caches_only_redacted_exception_text() -> None:
    error = ValueError(BOT_TOKEN)
    record = logging.LogRecord(
        "schedule_bot",
        logging.ERROR,
        __file__,
        1,
        "failed",
        (),
        (type(error), error, None),
    )
    SecretSafeFormatter(LOG_FORMAT).format(record)
    assert record.exc_text is not None
    assert BOT_TOKEN not in record.exc_text
    assert "ValueError: [REDACTED]" in record.exc_text


def test_console_logging_sanitizes_existing_handlers(monkeypatch) -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [handler])
    monkeypatch.setattr(root, "level", logging.INFO)
    monkeypatch.setattr(sys, "excepthook", sys.__excepthook__)
    configure_console_logging()
    root.info("HTTP Request: GET %s", "https://source.test/9006/?token=secret")
    assert "secret" not in stream.getvalue()
    assert "source.test/9006/" in stream.getvalue()
    assert root.handlers == [handler]


def test_unhandled_exception_hook_masks_chained_httpx_cause(monkeypatch) -> None:
    stream = io.StringIO()
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [logging.StreamHandler(stream)])
    monkeypatch.setattr(root, "level", logging.INFO)
    monkeypatch.setattr(sys, "excepthook", sys.__excepthook__)
    configure_console_logging()
    try:
        try:
            raise httpx.ProxyError(
                "socks5://proxy-user:proxy-secret@proxy.test:1080?key=query-secret"
            )
        except httpx.ProxyError as cause:
            raise RuntimeError(f"CLI request failed for {BOT_TOKEN}") from cause
    except RuntimeError as error:
        sys.excepthook(type(error), error, error.__traceback__)
    output = stream.getvalue()
    for secret in ("proxy-user", "proxy-secret", "query-secret", BOT_TOKEN):
        assert secret not in output
    assert "Unhandled exception" in output
    assert "ProxyError" in output
    assert "RuntimeError" in output
    assert "proxy.test:1080" in output
    assert "The above exception was the direct cause" in output


def test_interrupt_hook_retains_plain_traceback_and_masks_secrets(monkeypatch) -> None:
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stream)
    monkeypatch.setattr(sys, "excepthook", sys.__excepthook__)
    monkeypatch.setattr(logging.getLogger(), "handlers", [])
    configure_console_logging()
    error = KeyboardInterrupt(BOT_TOKEN)
    sys.excepthook(type(error), error, None)
    assert stream.getvalue() == "KeyboardInterrupt: [REDACTED]\n"


def test_unhandled_exception_hook_has_safe_handler_failure_fallback(
    monkeypatch,
) -> None:
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stream)
    monkeypatch.setattr(sys, "excepthook", sys.__excepthook__)
    monkeypatch.setattr(logging.getLogger(), "handlers", [])
    configure_console_logging()

    def fail_log(*args, **kwargs):
        raise OSError("log volume unavailable")

    monkeypatch.setattr(
        logging.getLogger("schedule_bot.logging_setup"), "critical", fail_log
    )
    error = ValueError("https://user:proxy-secret@proxy.test/path?key=query-secret")
    sys.excepthook(type(error), error, None)
    output = stream.getvalue()
    assert "ValueError" in output
    assert "proxy.test/path" in output
    assert "proxy-secret" not in output
    assert "query-secret" not in output


def test_persistent_logging_redacts_credentials(tmp_path) -> None:
    logger = logging.getLogger("schedule_bot.test_safe_persistent_log")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    path = tmp_path / "bot.log"
    handler = configure_file_logging(path, retention_days=90, logger=logger)
    try:
        logger.error(
            "API request failed for %s",
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        )
        logger.error("Proxy failed for %s", "http://user:password@proxy.test:8080")
    finally:
        logger.removeHandler(handler)
        handler.close()
    output = path.read_text(encoding="utf-8")
    assert BOT_TOKEN not in output
    assert "password" not in output
    assert "proxy.test:8080" in output
    assert "api.telegram.org/bot[REDACTED]/sendMessage" in output
