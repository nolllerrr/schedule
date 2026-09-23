from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from schedule_bot.domain import ImportResult, ScheduleChange
from schedule_bot.downloader import ScheduleDownloader
from schedule_bot.parser import ExcelScheduleParser
from schedule_bot.presentation import (
    format_changes,
    format_date,
    format_schedule,
    format_week,
)
from schedule_bot.repository import ScheduleRepository


class ScheduleUpdater:
    def __init__(
        self,
        downloader: ScheduleDownloader,
        parser: ExcelScheduleParser,
        repository: ScheduleRepository,
    ) -> None:
        self.downloader = downloader
        self.parser = parser
        self.repository = repository

    async def update_once(self) -> ImportResult:
        downloaded = await self.downloader.fetch_latest()
        parsed = await asyncio.to_thread(
            self.parser.parse,
            downloaded.path,
            source_filename=downloaded.link.filename,
        )
        return await asyncio.to_thread(
            self.repository.import_schedule,
            parsed,
            sha256=downloaded.sha256,
            source_url=downloaded.link.url,
        )

    def import_local(self, path: str | Path) -> ImportResult:
        source_path = Path(path)
        digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
        parsed = self.parser.parse(source_path)
        return self.repository.import_schedule(parsed, sha256=digest)


def changes_for_profile(
    changes: tuple[ScheduleChange, ...], *, role: str, target: str
) -> tuple[ScheduleChange, ...]:
    needle = target.casefold()
    result: list[ScheduleChange] = []
    for change in changes:
        lessons = tuple(item for item in (change.before, change.after) if item)
        if role == "student" and any(item.group_name == target for item in lessons):
            result.append(change)
        elif role == "teacher" and any(
            item.teacher and needle in item.teacher.casefold() for item in lessons
        ):
            result.append(change)
    return tuple(result)
