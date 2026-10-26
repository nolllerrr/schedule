"""Prepare schedule media without sending or acknowledging Telegram messages."""
from __future__ import annotations

import logging
from datetime import date
from collections.abc import Sequence

from aiogram.types import BufferedInputFile, InputMediaPhoto

from schedule_bot.card_service import get_card_service
from schedule_bot.domain import Lesson, LessonSegment, ScheduleChange


logger = logging.getLogger(__name__)


def snapshot_lessons(payload: dict) -> list[Lesson]:
    lessons = []
    for item in payload["lessons"]:
        row = dict(item)
        row["lesson_date"] = date.fromisoformat(row["lesson_date"])
        row["segments"] = tuple(LessonSegment(**segment) for segment in row.get("segments", ()))
        lessons.append(Lesson(**row))
    return lessons


def snapshot_changes(payload: dict) -> list[ScheduleChange]:
    return [ScheduleChange(item["kind"], *[
        snapshot_lessons({"lessons": [item[side]]})[0] if item[side] is not None else None
        for side in ("before", "after")
    ]) for item in payload["changes"]]


async def change_photo(
    changes: Sequence[ScheduleChange], *, role: str, target: str, lesson_date: date,
) -> InputMediaPhoto | None:
    try:
        png = await get_card_service().get_change_card(
            role=role, target=target, lesson_date=lesson_date, changes=changes,
        )
    except Exception as error:
        logger.info("Change media unavailable (%s)", type(error).__name__)
        return None
    return InputMediaPhoto(
        media=BufferedInputFile(png, filename=f"changes-{lesson_date.isoformat()}.png"),
        caption="Расписание изменилось", parse_mode=None,
    )


async def schedule_photo(
    lessons: Sequence[Lesson], *, role: str, target: str, lesson_date: date,
    caption: str | None = None,
) -> InputMediaPhoto | None:
    try:
        png = await get_card_service().get_card(
            role=role, target=target, lesson_date=lesson_date, lessons=lessons,
        )
    except Exception as error:
        # Rendering failure is safe to fall back from. Network errors belong
        # to the caller's delivery/retry flow, after the prepared file is sent.
        logger.info("Schedule media unavailable (%s)", type(error).__name__)
        return None
    return InputMediaPhoto(
        media=BufferedInputFile(png, filename=f"schedule-{lesson_date.isoformat()}.png"),
        caption=caption, parse_mode=None,
    )
