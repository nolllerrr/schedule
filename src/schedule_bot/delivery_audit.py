"""Durable evidence of individual external sends; this is not idempotency."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Literal

from schedule_bot.repository import ScheduleRepository
from schedule_bot.security import redact_secrets


AttemptOutcome = Literal["uncertain", "accepted", "rejected", "sent"]


def _timestamp(moment: datetime | None) -> str:
    value = moment or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def begin_attempt(
    repository: ScheduleRepository, *, notification_id: int | None = None,
    admin_notification_id: int | None = None, now: datetime | None = None,
) -> int:
    """Commit an uncertain attempt before starting an external API request."""
    if (notification_id is None) == (admin_notification_id is None):
        raise ValueError("An attempt requires exactly one outbox reference")
    repository.initialize()
    timestamp = _timestamp(now)
    with repository._connect() as connection:
        cursor = connection.execute(
            "INSERT INTO delivery_attempts(notification_id, admin_notification_id, started_at) "
            "VALUES (?, ?, ?)",
            (notification_id, admin_notification_id, timestamp),
        )
        connection.commit()
        return int(cursor.lastrowid)


def review_attempt(
    repository: ScheduleRepository, attempt_id: int, reason: str, *,
    now: datetime | None = None,
) -> bool:
    """Mark uncertainty reviewed without inventing a Telegram outcome or retrying."""
    if not reason.strip():
        raise ValueError("Review requires a non-empty reason")
    safe_reason = redact_secrets(reason.strip())[:500]
    repository.initialize()
    with repository._connect() as connection:
        cursor = connection.execute(
            "UPDATE delivery_attempts SET reviewed_at = ?, review_reason = ? "
            "WHERE id = ? AND outcome IN ('uncertain','accepted') AND reviewed_at IS NULL",
            (_timestamp(now), safe_reason, attempt_id),
        )
        connection.commit()
        return cursor.rowcount > 0


def unresolved_attempts(repository: ScheduleRepository, *, limit: int = 100) -> list[dict]:
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("Attempt limit must be positive")
    repository.initialize()
    with repository._connect() as connection:
        return [dict(row) for row in connection.execute(
            "SELECT id,notification_id,admin_notification_id,started_at,completed_at,"
            "outcome,telegram_message_id,error_category FROM delivery_attempts "
            "WHERE outcome IN ('uncertain','accepted') AND reviewed_at IS NULL "
            "ORDER BY id LIMIT ?", (limit,),
        )]


def record_attempt(
    repository: ScheduleRepository, attempt_id: int, outcome: AttemptOutcome, *,
    telegram_message_id: int | None = None, error_category: str | None = None,
    now: datetime | None = None,
) -> None:
    """Keep only machine categories and Telegram's message identifier."""
    if outcome not in {"uncertain", "accepted", "rejected", "sent"}:
        raise ValueError("Unsupported delivery attempt outcome")
    if error_category is not None and not re.fullmatch(r"[A-Za-z0-9_]{1,80}", error_category):
        raise ValueError("An error category must be a safe machine identifier")
    if telegram_message_id is not None and (
        not isinstance(telegram_message_id, int) or isinstance(telegram_message_id, bool)
    ):
        raise ValueError("Telegram message identifiers must be integers")
    repository.initialize()
    timestamp = _timestamp(now)
    with repository._connect() as connection:
        connection.execute(
            "UPDATE delivery_attempts SET outcome = ?, completed_at = ?, "
            "telegram_message_id = COALESCE(?, telegram_message_id), error_category = ? "
            "WHERE id = ? AND outcome != 'sent'",
            (outcome, timestamp, telegram_message_id, error_category, attempt_id),
        )
        connection.commit()
