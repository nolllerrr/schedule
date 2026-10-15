"""Keep a rotated copy of bot logs on the persistent data volume."""

from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from traceback import format_exception
from types import TracebackType

from schedule_bot.security import redact_secrets


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class SecretSafeFormatter(logging.Formatter):
    """Keep complete tracebacks, including chained causes, without credentials."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_secrets(super().format(record))

    def formatException(
        self, ei: tuple[type[BaseException], BaseException, TracebackType | None]
    ) -> str:
        # Formatter caches exc_text on the record, so cache only safe text.
        return redact_secrets(super().formatException(ei))

    def formatStack(self, stack_info: str) -> str:
        return redact_secrets(super().formatStack(stack_info))


def _log_unhandled_exception(
    exception_type: type[BaseException],
    exception: BaseException,
    traceback: TracebackType | None,
) -> None:
    if issubclass(exception_type, KeyboardInterrupt):
        # Retain the interpreter's usual interrupt traceback/exit behavior,
        # including secret protection if the interrupt has a chained cause.
        sys.stderr.write(
            redact_secrets(
                "".join(format_exception(exception_type, exception, traceback))
            )
        )
        return
    try:
        logging.getLogger(__name__).critical(
            "Unhandled exception", exc_info=(exception_type, exception, traceback)
        )
    except Exception:
        # A failed handler must not fall back to the raw interpreter traceback.
        sys.stderr.write(
            redact_secrets(
                "".join(format_exception(exception_type, exception, traceback))
            )
        )


def configure_console_logging(*, level: int = logging.INFO) -> None:
    logging.basicConfig(level=level, format=LOG_FORMAT)
    # basicConfig can reuse existing handlers; ensure these are safe as well.
    for handler in logging.getLogger().handlers:
        handler.setFormatter(SecretSafeFormatter(LOG_FORMAT))
    sys.excepthook = _log_unhandled_exception


def configure_file_logging(
    path: Path,
    *,
    retention_days: int,
    logger: logging.Logger | None = None,
) -> TimedRotatingFileHandler:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = TimedRotatingFileHandler(
        path,
        when="midnight",
        interval=1,
        backupCount=retention_days,
        encoding="utf-8",
        utc=True,
    )
    handler.setFormatter(SecretSafeFormatter(LOG_FORMAT))
    (logger or logging.getLogger()).addHandler(handler)
    return handler
