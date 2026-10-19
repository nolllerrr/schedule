"""Decide which imported changes warrant a Telegram notification."""

from __future__ import annotations

import re

from schedule_bot.domain import Lesson, ScheduleChange


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
            before.lesson_label != after.lesson_label,
            before.lesson_number != after.lesson_number,
            before.position != after.position,
            before.start_time != after.start_time,
            before.end_time != after.end_time,
            _assignment_key(before) != _assignment_key(after),
        )
    )


def _assignment_key(lesson: Lesson) -> tuple:
    return tuple(
        (_subject_key(part.subject), part.teacher, part.room, part.subgroup)
        for part in lesson.effective_segments()
    )


def changes_for_profile(
    changes: tuple[ScheduleChange, ...], *, role: str, target: str
) -> tuple[ScheduleChange, ...]:
    if role == "student":
        return tuple(
            change for change in changes
            if any(
                lesson.group_name == target
                for lesson in (change.before, change.after) if lesson is not None
            )
        )
    result: list[ScheduleChange] = []
    for change in changes:
        before = change.before.for_teacher(target) if change.before else None
        after = change.after.for_teacher(target) if change.after else None
        if before is None and after is None:
            continue
        kind = change.kind
        if before is None:
            kind = "added"
        elif after is None:
            kind = "removed"
        projected = ScheduleChange(kind, before, after)
        if should_notify(projected):
            result.append(projected)
    return tuple(result)
