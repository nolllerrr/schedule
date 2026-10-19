"""Deterministic comparison of slots without mistaking shifted rows for subjects."""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from schedule_bot.domain import Lesson, ScheduleChange


def _content(lesson: Lesson) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (part.subject, part.teacher, part.room, part.subgroup)
        for part in lesson.effective_segments()
    )


def _slot(lesson: Lesson) -> tuple[object, ...]:
    return (lesson.lesson_label, lesson.lesson_number, lesson.start_time, lesson.end_time)


def compare_lessons(before: Iterable[Lesson], after: Iterable[Lesson]) -> tuple[ScheduleChange, ...]:
    groups: dict[tuple[object, ...], tuple[list[Lesson], list[Lesson]]] = defaultdict(
        lambda: ([], [])
    )
    for side, lessons in enumerate((before, after)):
        for lesson in lessons:
            groups[(lesson.lesson_date, lesson.group_name)][side].append(lesson)
    changes: list[ScheduleChange] = []
    for key in sorted(groups):
        old, new = groups[key]
        old = sorted(old, key=lambda item: item.position)
        new = sorted(new, key=lambda item: item.position)
        # First retain unchanged slots, including a purely geometric row shift.
        for previous in old[:]:
            exact = next((item for item in new if item.comparable_data() == previous.comparable_data()), None)
            if exact is not None:
                old.remove(previous)
                new.remove(exact)
        # Match unchanged contents across slots, treating duplicates as a multiset.
        for previous in old[:]:
            candidates = [item for item in new if _content(item) == _content(previous)]
            if not candidates:
                continue
            current = min(candidates, key=lambda item: (abs(item.position - previous.position), item.position))
            old.remove(previous)
            new.remove(current)
            if _slot(previous) != _slot(current):
                changes.append(ScheduleChange("moved", previous, current))
        # Unmatched replacements in the same slot remain ordinary modifications.
        for previous in old[:]:
            current = next((item for item in new if item.position == previous.position), None)
            if current is not None:
                old.remove(previous)
                new.remove(current)
                changes.append(ScheduleChange("modified", previous, current))
        changes.extend(ScheduleChange("removed", item, None) for item in old)
        changes.extend(ScheduleChange("added", None, item) for item in new)
    return tuple(sorted(changes, key=lambda item: (
        item.lesson_date, item.group_name,
        (item.after or item.before).position, item.kind,
    )))
