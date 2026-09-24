"""Decide which imported changes warrant a Telegram notification."""

from __future__ import annotations

import re

from schedule_bot.domain import ScheduleChange


_MDK_COURSE_PROJECT = re.compile(
    r"(МДК\s*\.\s*\d{2}\s*\.\s*\d{2}\s*\.?)\s+КП\b",
    flags=re.IGNORECASE,
)


def subject_changed(before: str, after: str) -> bool:
    """Treat a course-project label after the same MDK code as a clarification."""
    return _subject_key(before) != _subject_key(after)


def _subject_key(value: str) -> str:
    without_marker = _MDK_COURSE_PROJECT.sub(r"\1", value)
    return " ".join(without_marker.split()).casefold()


def should_notify(change: ScheduleChange) -> bool:
    if change.kind != "modified":
        return True
    before, after = change.before, change.after
    if before is None or after is None:
        return True
    return any(
        (
            subject_changed(before.subject, after.subject),
            before.lesson_label != after.lesson_label,
            before.lesson_number != after.lesson_number,
            before.start_time != after.start_time,
            before.end_time != after.end_time,
            before.teacher != after.teacher,
            before.room != after.room,
            before.subgroup != after.subgroup,
        )
    )
