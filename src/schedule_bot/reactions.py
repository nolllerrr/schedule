"""Bounded best-effort comments after confirmed schedule delivery."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import hashlib
import html
import logging
import re
from time import monotonic
from typing import TYPE_CHECKING

from schedule_bot.notifier import _destination_lock
from schedule_grok import generate_grok_reaction

if TYPE_CHECKING:
    from aiogram import Bot
    from schedule_bot.config import Settings
    from schedule_bot.domain import ImportResult
    from schedule_bot.repository import ScheduleRepository

logger = logging.getLogger(__name__)
JOB_TTL_SECONDS = 300
MAX_PENDING_JOBS = 8
POLL_SECONDS = 0.5


def _strip_html(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def comment_chats_for_group(repository, settings, target):
    """Read only bounded, enabled group destinations allowed to receive comments."""
    ids = tuple(settings.grok_reaction_chat_ids)
    if len(ids) > 64:
        raise ValueError("Grok allowlist exceeds 64 chats")
    types = tuple(
        t for t in settings.grok_reaction_chat_types if t in {"group", "supergroup"}
    )
    if not settings.grok_reactions_enabled or not ids or not types:
        return []
    repository.initialize()
    with repository._connect() as connection:
        rows = connection.execute(
            f"""SELECT * FROM chat_profiles
                WHERE chat_id IN ({','.join('?' for _ in ids)})
                  AND chat_type IN ({','.join('?' for _ in types)})
                  AND target=? AND active=1 AND notifications=1
                ORDER BY chat_id LIMIT 64""",
            (*ids, *types, target),
        )
        return [dict(row) for row in rows]


def _get_import_deliveries(repository, import_id, settings, delivery_id=None):
    """Read at most 64 allowlisted chats, never materialize private recipients."""
    ids = tuple(settings.grok_reaction_chat_ids)
    if len(ids) > 64:
        raise ValueError("Grok allowlist exceeds 64 chats")
    types = tuple(
        t for t in settings.grok_reaction_chat_types if t in {"group", "supergroup"}
    )
    if not ids or not types:
        return []
    repository.initialize()
    with repository._connect() as connection:
        rows = connection.execute(
            f"""SELECT d.id, d.destination_id, d.chat_type, d.text, d.target_snapshot,
                CASE WHEN EXISTS (
                    SELECT 1 FROM notification_outbox part
                    WHERE part.import_id=d.import_id AND part.destination_kind='chat'
                      AND part.destination_id=d.destination_id AND part.status='failed'
                ) THEN 'failed' WHEN EXISTS (
                    SELECT 1 FROM notification_outbox part
                    WHERE part.import_id=d.import_id AND part.destination_kind='chat'
                      AND part.destination_id=d.destination_id AND part.status!='sent'
                ) THEN 'pending' ELSE 'sent' END AS series_status
                FROM notification_outbox d JOIN chat_profiles p ON p.chat_id=d.destination_id
                WHERE d.import_id=? AND d.destination_kind='chat' AND d.chunk_index=0
                  AND d.destination_id IN ({",".join("?" for _ in ids)})
                  AND d.chat_type IN ({",".join("?" for _ in types)})
                  AND p.chat_type=d.chat_type AND p.active=1 AND p.notifications=1
                  AND p.target=d.target_snapshot AND d.expires_at>?
                  AND (? IS NULL OR d.id=?)
                  AND NOT EXISTS (
                    SELECT 1 FROM notification_outbox newer
                    WHERE newer.destination_kind='chat' AND newer.destination_id=d.destination_id
                      AND newer.chunk_index=0 AND newer.import_id>d.import_id
                  )
                ORDER BY d.destination_id LIMIT 64""",
            (
                import_id,
                *ids,
                *types,
                repository._utc_timestamp(None),
                delivery_id,
                delivery_id,
            ),
        )
        return [dict(row) for row in rows]


async def _current(repository, import_id, settings, delivery_id):
    rows = await asyncio.to_thread(
        _get_import_deliveries, repository, import_id, settings, delivery_id
    )
    return rows[0] if rows and rows[0]["series_status"] != "failed" else None


async def _dispatch_import(bot, repository, settings, import_id, deadline):
    rows = await asyncio.to_thread(
        _get_import_deliveries, repository, import_id, settings
    )
    cache = {}
    for original in rows:
        delivery_id = original["id"]
        current = await _current(repository, import_id, settings, delivery_id)
        while current and current["series_status"] != "sent" and monotonic() < deadline:
            await asyncio.sleep(min(POLL_SECONDS, max(0, deadline - monotonic())))
            current = await _current(repository, import_id, settings, delivery_id)
        if not current or current["series_status"] != "sent" or monotonic() >= deadline:
            continue
        text = _strip_html(str(current["text"]))
        if not text:
            continue
        reaction = cache.get(text)
        if text not in cache:
            generation = asyncio.create_task(
                generate_grok_reaction(
                    text,
                    enabled=True,
                    cli_path=settings.grok_cli_path,
                    timeout=min(settings.grok_timeout_seconds, deadline - monotonic()),
                )
            )
            try:
                while not generation.done():
                    await asyncio.wait({generation}, timeout=POLL_SECONDS)
                    current = await _current(
                        repository, import_id, settings, delivery_id
                    )
                    if (
                        not current
                        or current["series_status"] != "sent"
                        or monotonic() >= deadline
                    ):
                        generation.cancel()
                        break
                reaction = await generation
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
                continue
            finally:
                generation.cancel()
                await asyncio.gather(generation, return_exceptions=True)
            cache[text] = reaction
        if not reaction:
            continue
        chat_id = int(original["destination_id"])
        async with _destination_lock(repository, "chat", chat_id):
            current = await _current(repository, import_id, settings, delivery_id)
            if (
                not current
                or current["series_status"] != "sent"
                or monotonic() >= deadline
            ):
                continue
            try:
                await bot.send_message(chat_id, reaction, parse_mode=None)
            except asyncio.CancelledError:
                logger.info(
                    "Grok comment interrupted import=%s delivery=%s",
                    import_id,
                    delivery_id,
                )
                raise
            except Exception as error:
                logger.warning(
                    "Grok comment failed import=%s delivery=%s category=%s",
                    import_id,
                    delivery_id,
                    type(error).__name__,
                )
            else:
                logger.info(
                    "Grok comment sent import=%s delivery=%s chars=%s hash=%s",
                    import_id,
                    delivery_id,
                    len(reaction),
                    hashlib.sha256(reaction.encode()).hexdigest()[:16],
                )


async def dispatch_grok_reactions(bot, repository, settings, result):
    if (
        not settings.grok_reactions_enabled
        or not settings.grok_reaction_chat_ids
        or result.skipped
        or not result.import_id
        or not (result.new_dates or result.changes)
    ):
        return
    try:
        async with asyncio.timeout(JOB_TTL_SECONDS):
            await _dispatch_import(
                bot,
                repository,
                settings,
                result.import_id,
                monotonic() + JOB_TTL_SECONDS,
            )
    except Exception as error:
        logger.warning(
            "Grok job dropped import=%s category=%s",
            result.import_id,
            type(error).__name__,
        )


class GrokReactionWorker:
    """One runtime worker; lightweight bounded jobs are intentionally not durable."""

    def __init__(self, bot: Bot, repository: ScheduleRepository, settings: Settings):
        self.bot, self.repository, self.settings = bot, repository, settings
        self.queue = asyncio.Queue(maxsize=MAX_PENDING_JOBS)
        self.seen = OrderedDict()

    def enqueue(self, result: ImportResult) -> None:
        if (
            not self.settings.grok_reactions_enabled
            or not self.settings.grok_reaction_chat_ids
            or result.skipped
            or not result.import_id
            or not (result.new_dates or result.changes)
            or result.import_id in self.seen
        ):
            return
        self.seen[result.import_id] = None
        if len(self.seen) > 128:
            self.seen.popitem(last=False)
        if self.queue.full():
            self.queue.get_nowait()
            self.queue.task_done()
        self.queue.put_nowait((result.import_id, monotonic() + JOB_TTL_SECONDS))

    async def run(self) -> None:
        while True:
            import_id, deadline = await self.queue.get()
            try:
                remaining = deadline - monotonic()
                if remaining > 0:
                    async with asyncio.timeout(remaining):
                        await _dispatch_import(
                            self.bot,
                            self.repository,
                            self.settings,
                            import_id,
                            deadline,
                        )
            except Exception as error:
                logger.warning(
                    "Grok job dropped import=%s category=%s",
                    import_id,
                    type(error).__name__,
                )
            finally:
                self.queue.task_done()
