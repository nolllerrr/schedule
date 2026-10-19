"""Terminate a standard workbook parser process on deadline or cancellation."""
from __future__ import annotations

import asyncio
import multiprocessing
import pickle
import tempfile
from pathlib import Path

from schedule_bot.parser import ExcelScheduleParser
from schedule_bot.parser.excel import ScheduleParseError


def _parse_worker(path, source_filename, limits, result_path):
    try:
        parsed = ExcelScheduleParser(limits=limits).parse(path, source_filename=source_filename)
        payload = (True, parsed)
    except Exception as error:
        payload = (False, (type(error).__name__, str(error)))
    with open(result_path, "wb") as result:
        pickle.dump(payload, result, protocol=pickle.HIGHEST_PROTOCOL)


async def parse_with_budget(path: Path, *, source_filename: str | None = None, limits=None, timeout_seconds: float = 60, _target=None):
    if not 0 < timeout_seconds <= 300:
        raise ValueError("Parser deadline must be between 0 and 300 seconds")
    context = multiprocessing.get_context("spawn")
    temporary = tempfile.TemporaryDirectory(prefix="schedule-parse-")
    result_path = Path(temporary.name) / "result.pickle"
    process = context.Process(target=_target or _parse_worker, args=(path, source_filename, limits, result_path), daemon=True)
    try:
        process.start()
        try:
            async with asyncio.timeout(timeout_seconds):
                while process.is_alive():
                    await asyncio.sleep(0.02)
                if process.exitcode != 0 or not result_path.exists():
                    raise ScheduleParseError("Workbook parser process exited without a result")
                def read_result():
                    # Only the trusted child writes this private temporary file.
                    with result_path.open("rb") as result:
                        return pickle.load(result)
                success, payload = await asyncio.to_thread(read_result)
        except TimeoutError as cause:
            raise ScheduleParseError("Workbook parsing deadline exceeded") from cause
        if not success:
            raise ScheduleParseError(f"{payload[0]}: {payload[1]}")
        return payload
    finally:
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
            await asyncio.to_thread(process.join, 5)
            if process.is_alive():
                process.kill()
                await asyncio.to_thread(process.join, 5)
        process.close()
        temporary.cleanup()
