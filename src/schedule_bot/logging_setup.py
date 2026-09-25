"""Keep a rotated copy of bot logs on the persistent data volume."""

from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


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
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    (logger or logging.getLogger()).addHandler(handler)
    return handler
