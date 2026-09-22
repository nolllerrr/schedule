from __future__ import annotations

import logging
from datetime import date
from typing import Literal, cast

from aiogram import Bot

from schedule_bot.domain import ImportResult
from schedule_bot.repository import ScheduleRepository
from schedule_bot.services import (
    changes_for_profile,
    format_changes,
    format_schedule,
)


logger = logging.getLogger(__name__)


class ScheduleNotifier:
    def __init__(
        self,
        bot: Bot,
        repository: ScheduleRepository,
        admin_ids: tuple[int, ...],
    ) -> None:
        self.bot = bot
        self.repository = repository
        self.admin_ids = admin_ids

    async def notify_import(self, result: ImportResult) -> None:
        if result.skipped or not result.changes:
            return
        for profile in self.repository.subscribers():
            role = cast(Literal["student", "teacher"], profile["role"])
            target = str(profile["target"])
            changes = changes_for_profile(
                result.changes, role=role, target=target
            )
            if not changes:
                continue
            dates = sorted({change.lesson_date for change in changes})
            only_additions = all(change.kind == "added" for change in changes)
            if only_additions:
                parts = ["<b>Опубликовано новое расписание</b>"]
                for lesson_date in dates:
                    lessons = self.repository.lessons_for(
                        role=role,
                        target=target,
                        lesson_date=lesson_date,
                    )
                    parts.append(
                        format_schedule(
                            lessons,
                            target=target,
                            lesson_date=lesson_date,
                            role=role,
                        )
                    )
                text = "\n\n".join(parts)
            else:
                text = format_changes(changes)
            await self._send_chunks(int(profile["user_id"]), text)

    async def notify_admins_error(self, error: Exception) -> None:
        text = (
            "Ошибка автоматического обновления расписания: "
            f"{type(error).__name__}: {error}"
        )
        for admin_id in self.admin_ids:
            try:
                await self.bot.send_message(admin_id, text)
            except Exception:
                logger.exception("Could not notify administrator %s", admin_id)

    async def _send_chunks(self, user_id: int, text: str) -> None:
        chunks = _split_message(text)
        for chunk in chunks:
            try:
                await self.bot.send_message(user_id, chunk)
            except Exception:
                logger.exception("Could not notify user %s", user_id)
                break


def _split_message(text: str, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(paragraph) > limit:
            chunks.append(paragraph[:limit])
            paragraph = paragraph[limit:]
        current = paragraph
    if current:
        chunks.append(current)
    return chunks

