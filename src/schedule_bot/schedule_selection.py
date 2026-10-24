"""Select the study day relevant at the supplied local time."""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Literal

from schedule_bot.domain import Lesson
from schedule_bot.repository import ScheduleRepository


def current_schedule(
    repository: ScheduleRepository,
    *,
    role: Literal["student", "teacher"],
    target: str,
    now: datetime,
) -> tuple[date, list[Lesson]] | None:
    """Keep today until all lessons end, then select the next published study day.

    ``now`` must be in the bot's local timezone. Unknown end times keep today's
    schedule relevant until midnight rather than switching prematurely.
    """
    today = now.date()
    lessons = repository.lessons_for(role=role, target=target, lesson_date=today)
    if lessons and not _all_lessons_finished(lessons, now):
        return today, lessons

    for lesson_date in repository.published_dates():
        if lesson_date <= today:
            continue
        lessons = repository.lessons_for(
            role=role, target=target, lesson_date=lesson_date
        )
        if lessons:
            return lesson_date, lessons
    return None


def _all_lessons_finished(lessons: list[Lesson], now: datetime) -> bool:
    end_times: list[time] = []
    for lesson in lessons:
        if not lesson.end_time:
            return False
        try:
            end = time.fromisoformat(lesson.end_time)
        except ValueError:
            return False
        if end.tzinfo is not None:
            return False
        end_times.append(end)
    return bool(end_times) and now.time().replace(tzinfo=None) >= max(end_times)
