import asyncio
import multiprocessing
import shutil
from datetime import date
from types import SimpleNamespace

import pytest
from test_publication_regressions import active_and_outbox

from schedule_bot import services
from schedule_bot.downloader import DownloadedSchedule, ScheduleLink
from schedule_bot.parser import ExcelScheduleParser
from schedule_bot.parser.excel import ScheduleParseError
from schedule_bot.repository import ScheduleRepository
from schedule_bot.services import ScheduleUpdater


class BatchSource:
    def __init__(self, downloads):
        self.downloads = downloads

    async def find_all(self):
        return [download.link for download in self.downloads]

    async def download(self, link):
        return next(download for download in self.downloads if download.link == link)


def downloads(tmp_path, schedule_workbook, *, invalid=False):
    result = []
    for index, day in enumerate((21, 22)):
        filename = f"{day}.09.2026.xlsx"
        path = tmp_path / filename
        shutil.copyfile(schedule_workbook, path)
        if invalid and index == 1:
            path.write_bytes(b"invalid workbook")
        link = ScheduleLink(f"https://example.test/{index}", filename,
                            date(2026, 9, day), date(2026, 9, day))
        result.append(DownloadedSchedule(link, path, f"synthetic-{index}"))
    return result


@pytest.mark.asyncio
async def test_batch_uses_real_child_parser_and_filename_per_source(tmp_path, schedule_workbook):
    repository = ScheduleRepository(tmp_path / "batch.db")
    before = {child.pid for child in multiprocessing.active_children()}
    result = await ScheduleUpdater(BatchSource(downloads(tmp_path, schedule_workbook)),
                                   ExcelScheduleParser(), repository).update_once()
    assert not result.skipped
    with repository._connect() as connection:
        rows = connection.execute("SELECT source_filename FROM imports ORDER BY source_filename").fetchall()
    assert {"21.09.2026.xlsx", "22.09.2026.xlsx"} <= {row[0] for row in rows}
    assert "21.09.2026.xlsx; 22.09.2026.xlsx" in {row[0] for row in rows}
    assert repository.lessons_for(role="student", target="ПД-12", lesson_date=date(2026, 9, 21))
    assert repository.lessons_for(role="student", target="ПД-12", lesson_date=date(2026, 9, 22))
    assert {child.pid for child in multiprocessing.active_children()} <= before


@pytest.mark.asyncio
async def test_bad_second_workbook_cannot_partially_activate_batch(tmp_path, schedule_workbook):
    repository = ScheduleRepository(tmp_path / "batch.db")
    repository.save_profile(123, role="student", target="ПД-12")
    parsed = ExcelScheduleParser().parse(schedule_workbook)
    repository.import_schedule(parsed, sha256="before")
    before = active_and_outbox(repository)
    children = {child.pid for child in multiprocessing.active_children()}
    with pytest.raises(ScheduleParseError):
        await ScheduleUpdater(BatchSource(downloads(tmp_path, schedule_workbook, invalid=True)),
                              ExcelScheduleParser(), repository).update_once()
    assert active_and_outbox(repository) == before
    assert {child.pid for child in multiprocessing.active_children()} <= children


@pytest.mark.asyncio
async def test_batch_deadline_after_first_child_preserves_active_data(tmp_path, schedule_workbook, monkeypatch):
    repository = ScheduleRepository(tmp_path / "batch.db")
    repository.save_profile(123, role="student", target="ПД-12")
    repository.import_schedule(ExcelScheduleParser().parse(schedule_workbook), sha256="before")
    before = active_and_outbox(repository)
    children = {child.pid for child in multiprocessing.active_children()}
    times = iter((0, 0, 121))
    monkeypatch.setattr(services, "asyncio", SimpleNamespace(
        timeout=asyncio.timeout, to_thread=asyncio.to_thread,
        get_running_loop=lambda: SimpleNamespace(time=lambda: next(times)),
    ))
    with pytest.raises(ScheduleParseError, match="batch deadline"):
        await ScheduleUpdater(BatchSource(downloads(tmp_path, schedule_workbook)),
                              ExcelScheduleParser(), repository).update_once()
    assert active_and_outbox(repository) == before
    assert {child.pid for child in multiprocessing.active_children()} <= children
