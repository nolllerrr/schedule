"""Build delivery text from the import snapshot, without database or API calls."""

from __future__ import annotations

from datetime import date
from typing import Iterator

from schedule_bot.domain import Lesson, ScheduleChange
from schedule_bot.message_utils import split_message
from schedule_bot.presentation import format_changes, format_schedule


def notification_deliveries(
    changes: tuple[ScheduleChange, ...],
    *,
    role: str,
    target: str,
    new_dates: tuple[date, ...],
    lessons: tuple[Lesson, ...],
) -> Iterator[dict]:
    for lesson_date in sorted({change.lesson_date for change in changes}):
        day_changes = tuple(
            change for change in changes if change.lesson_date == lesson_date
        )
        payload = None
        if lesson_date in new_dates:
            day_lessons = []
            for lesson in lessons:
                if lesson.lesson_date != lesson_date:
                    continue
                if role == "student":
                    if lesson.group_name == target:
                        day_lessons.append(lesson)
                elif (projected := lesson.for_teacher(target)) is not None:
                    day_lessons.append(projected)
            text = "<b>Опубликовано новое расписание</b>\n\n" + format_schedule(
                day_lessons, target=target, lesson_date=lesson_date, role=role
            )
            if day_lessons:
                payload = {
                    "role": role, "target": target, "date": lesson_date.isoformat(),
                    "lessons": [{key: value for key, value in lesson.to_dict().items() if key != "raw_data"}
                                for lesson in day_lessons],
                }
        else:
            text = format_changes(day_changes)
        chunks = split_message(text)
        for index, chunk in enumerate(chunks):
            media = None
            if payload is not None:
                media = {key: value for key, value in payload.items() if key != "lessons"}
                media.update(render=index == 0, last=index == len(chunks) - 1)
                if index == 0:
                    media.update(lessons=payload["lessons"], following_chunks=len(chunks) - 1)
            yield {"text": chunk, "card_payload": media}


def notification_chunks(changes, *, role, target, new_dates, lessons) -> Iterator[str]:
    for item in notification_deliveries(
        changes, role=role, target=target, new_dates=new_dates, lessons=lessons,
    ):
        yield item["text"]
