from __future__ import annotations

import asyncio
import multiprocessing
import time

import pytest

from schedule_bot.parse_budget import parse_with_budget
from schedule_bot.parser import ExcelScheduleParser
from schedule_bot.parser.excel import ScheduleParseError


def hanging_parser(path, filename, limits, channel):
    time.sleep(20)


@pytest.mark.asyncio
async def test_process_parse_matches_standard_reader(schedule_workbook):
    expected = ExcelScheduleParser().parse(schedule_workbook)
    actual = await parse_with_budget(schedule_workbook)
    assert actual == expected


@pytest.mark.asyncio
async def test_deadline_terminates_parser_process(tmp_path):
    before = {process.pid for process in multiprocessing.active_children()}
    with pytest.raises(ScheduleParseError, match="deadline"):
        await parse_with_budget(tmp_path / "unused.xlsx", timeout_seconds=0.1, _target=hanging_parser)
    assert {process.pid for process in multiprocessing.active_children()} <= before


@pytest.mark.asyncio
async def test_cancellation_terminates_parser_process(tmp_path):
    before = {process.pid for process in multiprocessing.active_children()}
    task = asyncio.create_task(parse_with_budget(tmp_path / "unused.xlsx", _target=hanging_parser))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert {process.pid for process in multiprocessing.active_children()} <= before
