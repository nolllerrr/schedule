from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from schedule_bot.change_policy import changes_for_profile as changes_for_profile
from schedule_bot.domain import ImportResult
from schedule_bot.downloader import ScheduleDownloadError, ScheduleDownloader
from schedule_bot.parser import ExcelScheduleParser
from schedule_bot.parser.excel import ScheduleParseError
from schedule_bot.presentation import (
    format_changes as format_changes,
    format_date as format_date,
    format_schedule as format_schedule,
    format_week as format_week,
)
from schedule_bot.repository import ScheduleRepository
from schedule_bot.publications import PublicationInput
from schedule_bot.parse_budget import parse_with_budget

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

    async def update_once(self, *, automatic: bool = False, approval_reason: str | None = None) -> ImportResult:
        stage = "fetch"
        try:
            await asyncio.to_thread(self.repository.record_sync_check, phase="start")
            multi_source = callable(getattr(type(self.downloader), "find_all", None))
            try:
                if multi_source:
                    async with asyncio.timeout(300):
                        links = await self.downloader.find_all()
                        downloads = [await self.downloader.download(link) for link in links]
                else:
                    downloads = [await self.downloader.fetch_latest()]
            except TimeoutError as cause:
                error = ScheduleDownloadError("Source check deadline exceeded", transient=True, category="timeout")
                if automatic and self.outage_tracker is not None:
                    await self.outage_tracker.record_failure(error)
                raise error from cause
            except ScheduleDownloadError as error:
                if automatic and error.transient and self.outage_tracker is not None:
                    await self.outage_tracker.record_failure(error)
                raise
            await asyncio.to_thread(self.repository.record_sync_check, phase="fetch")
            if self.outage_tracker is not None:
                await self.outage_tracker.record_success()
            entries = []
            legacy_result = None
            parsing_deadline = asyncio.get_running_loop().time() + 120
            for downloaded in downloads:
                # Sheet selection depends on the filename's range as well as bytes.
                # Keep raw SHA first so downloaded-file retention can identify it.
                snapshot_key = (f"{downloaded.sha256}:{downloaded.link.start_date}:{downloaded.link.end_date}"
                                if multi_source else downloaded.sha256)
                existing = await asyncio.to_thread(self.repository.find_import, snapshot_key)
                if existing is not None:
                    parsed = None
                    legacy_result = existing
                else:
                    stage = "parse"
                    if multi_source and type(self.parser) is ExcelScheduleParser:
                        remaining = parsing_deadline - asyncio.get_running_loop().time()
                        if remaining <= 0:
                            raise ScheduleParseError("Publication parsing batch deadline exceeded")
                        parsed = await parse_with_budget(downloaded.path, source_filename=downloaded.link.filename, limits=self.parser.limits, timeout_seconds=min(60, remaining))
                    else:
                        parsed = await asyncio.to_thread(self.parser.parse, downloaded.path, source_filename=downloaded.link.filename)
                entries.append(PublicationInput(downloaded.link.url, snapshot_key, parsed))
            stage = "import"
            if multi_source:
                result = await asyncio.to_thread(self.repository.reconcile_publications, entries, approval_reason=approval_reason)
            elif legacy_result is not None:
                result = legacy_result
            else:
                result = await asyncio.to_thread(self.repository.import_schedule, entries[0].parsed, sha256=entries[0].sha256, source_url=entries[0].url)
            await asyncio.to_thread(self.repository.record_sync_check, phase="accepted", import_id=result.import_id)
            return result
        except Exception as error:
            error.schedule_stage = stage
            try:
                await asyncio.to_thread(self.repository.record_sync_check, phase="failed", error_code=getattr(error, "category", type(error).__name__))
            except Exception:
                logging.getLogger(__name__).exception("Could not record source check outcome")
            raise

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
