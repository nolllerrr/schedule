"""Optional Grok reaction service for schedule change notifications."""

from __future__ import annotations

import asyncio
import html
import logging
import re
from typing import TYPE_CHECKING

from schedule_grok import generate_grok_reaction

if TYPE_CHECKING:
    from aiogram import Bot
    from schedule_bot.config import Settings
    from schedule_bot.domain import ImportResult
    from schedule_bot.repository import ScheduleRepository

logger = logging.getLogger(__name__)


def _strip_html(text: str) -> str:
    """Strip HTML formatting tags and decode HTML entities from notification text."""
    clean = re.sub(r"<[^>]+>", "", text)
    return html.unescape(clean).strip()


async def dispatch_grok_reactions(
    bot: Bot,
    repository: ScheduleRepository,
    settings: Settings,
    result: ImportResult,
) -> None:
    """Generate and send Grok chat reactions for newly published schedules.

    Runs in the background without blocking regular schedule delivery.
    Failures in Grok or Telegram never break main bot operations.
    """
    if not settings.grok_reactions_enabled or not settings.grok_reaction_chat_ids:
        return
    if result.skipped or not result.import_id:
        return
    if not result.new_dates and not result.changes:
        return

    try:
        deliveries = await asyncio.to_thread(
            _get_import_deliveries, repository, result.import_id
        )
        if not deliveries:
            return

        allowed_types = set(settings.grok_reaction_chat_types)
        allowed_chat_ids = set(settings.grok_reaction_chat_ids)
        filtered = [
            d for d in deliveries
            if str(d["chat_type"]) in allowed_types
            and int(d["destination_id"]) in allowed_chat_ids
        ]
        if not filtered:
            return

        # Group destinations by first chunk text (the schedule summary/listing)
        destinations_by_text: dict[str, list[int]] = {}
        for item in filtered:
            text = str(item["text"])
            destinations_by_text.setdefault(text, []).append(int(item["destination_id"]))

        for raw_text, chat_ids in destinations_by_text.items():
            clean_text = _strip_html(raw_text)
            if not clean_text:
                continue

            reaction = await generate_grok_reaction(
                clean_text,
                enabled=settings.grok_reactions_enabled,
                cli_path=settings.grok_cli_path,
                timeout=settings.grok_timeout_seconds,
            )
            if not reaction:
                continue

            for chat_id in chat_ids:
                try:
                    await bot.send_message(chat_id, reaction)
                    logger.info("Sent Grok reaction to destination %s: %s", chat_id, reaction)
                except Exception:
                    logger.warning(
                        "Could not send Grok reaction to destination %s", chat_id, exc_info=True
                    )
    except Exception:
        logger.exception("Could not process Grok reactions for import %s", result.import_id)


def _get_import_deliveries(
    repository: ScheduleRepository, import_id: int
) -> list[dict[str, object]]:
    """Return initial notification chunks queued for the specified import."""
    with repository._connect() as connection:
        cursor = connection.execute(
            """
            SELECT destination_id, destination_kind, chat_type, text
            FROM notification_outbox
            WHERE import_id = ? AND chunk_index = 0
            """,
            (import_id,),
        )
        return [dict(row) for row in cursor.fetchall()]
