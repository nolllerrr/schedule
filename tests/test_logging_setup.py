from __future__ import annotations

import logging
import time

from schedule_bot.config import Settings
from schedule_bot.logging_setup import configure_file_logging


def test_persistent_log_survives_reopen_and_rotates(tmp_path) -> None:
    log_path = tmp_path / "data" / "logs" / "bot.log"
    logger = logging.getLogger("schedule_bot.test_persistent_log")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    handler = configure_file_logging(log_path, retention_days=90, logger=logger)
    try:
        logger.info("before rotation")
        handler.rolloverAt = int(time.time()) - 1
        logger.warning("after rotation")
        assert handler.backupCount == 90
    finally:
        logger.removeHandler(handler)
        handler.close()

    archives = list(log_path.parent.glob("bot.log.*"))
    assert len(archives) == 1
    assert "before rotation" in archives[0].read_text(encoding="utf-8")
    assert "after rotation" in log_path.read_text(encoding="utf-8")

    reopened = configure_file_logging(log_path, retention_days=90, logger=logger)
    try:
        logger.info("after restart")
    finally:
        logger.removeHandler(reopened)
        reopened.close()

    assert "after rotation" in log_path.read_text(encoding="utf-8")
    assert "after restart" in log_path.read_text(encoding="utf-8")


def test_log_retention_is_configurable(monkeypatch) -> None:
    monkeypatch.setenv("LOG_RETENTION_DAYS", "30")
    assert Settings.from_env(require_bot_token=False).log_retention_days == 30
