from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

from schedule_bot.change_policy import changes_for_profile as changes_for_profile
from schedule_bot.domain import ImportResult
from schedule_bot.downloader import ScheduleDownloadError, ScheduleDownloader
from schedule_bot.parser import ExcelScheduleParser
from schedule_bot.presentation import (
    format_changes as format_changes,
    format_date as format_date,
    format_schedule as format_schedule,
    format_week as format_week,
)
from schedule_bot.repository import ScheduleRepository

if TYPE_CHECKING:
    from schedule_bot.outage import ScheduleOutageTracker


class ScheduleUpdater:
    def __init__(
        self,
        downloader: ScheduleDownloader,
        parser: ExcelScheduleParser,
        repository: ScheduleRepository,
        *,
        outage_tracker: ScheduleOutageTracker | None = None,
    ) -> None:
        self.downloader = downloader
        self.parser = parser
        self.repository = repository
        self.outage_tracker = outage_tracker

    async def update_once(self, *, automatic: bool = False) -> ImportResult:
        try:
            downloaded = await self.downloader.fetch_latest()
        except ScheduleDownloadError as error:
            if automatic and error.transient and self.outage_tracker is not None:
                await self.outage_tracker.record_failure(error)
            raise
        if self.outage_tracker is not None:
            # Only a real successful page/workbook fetch restores the source.
            # Parsing/import/delivery failures below still reach critical alerts.
            await self.outage_tracker.record_success()
        existing = await asyncio.to_thread(
            self.repository.find_import, downloaded.sha256
        )
        if existing is not None:
            return existing
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

    def import_local(
        self, path: str | Path, *, approval_reason: str | None = None
    ) -> ImportResult:
        source_path = Path(path)
        if approval_reason is not None and not approval_reason.strip():
            raise ValueError("Approval requires a non-empty review reason")
        with source_path.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        existing = self.repository.find_import(digest)
        if existing is not None:
            return existing
        parsed = self.parser.parse(source_path)
        return self.repository.import_schedule(
            parsed, sha256=digest, approval_reason=approval_reason
        )
