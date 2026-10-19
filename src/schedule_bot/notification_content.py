"""Build delivery text from the import snapshot, without database or API calls."""

from __future__ import annotations

from datetime import date
from typing import Iterator

from schedule_bot.domain import Lesson, ScheduleChange
from schedule_bot.message_utils import split_message
from schedule_bot.presentation import format_changes, format_schedule


def notification_chunks(
    changes: tuple[ScheduleChange, ...],
    *,
    role: str,
    target: str,
    new_dates: tuple[date, ...],
    lessons: tuple[Lesson, ...],
) -> Iterator[str]:
    for lesson_date in sorted({change.lesson_date for change in changes}):
        day_changes = tuple(
            change for change in changes if change.lesson_date == lesson_date
        )
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
        else:
            text = format_changes(day_changes)
        yield from split_message(text)
