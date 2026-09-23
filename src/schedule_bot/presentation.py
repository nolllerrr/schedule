from __future__ import annotations

import html
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date

from schedule_bot.domain import Lesson, ScheduleChange


RUSSIAN_MONTHS = (
    "",
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)

RUSSIAN_WEEKDAYS = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)

NUMBER_EMOJI = {
    1: "1️⃣",
    2: "2️⃣",
    3: "3️⃣",
    4: "4️⃣",
    5: "5️⃣",
    6: "6️⃣",
    7: "7️⃣",
    8: "8️⃣",
    9: "9️⃣",
    10: "🔟",
}


def format_date(value: date) -> str:
    return (
        f"{value.day} {RUSSIAN_MONTHS[value.month]}, "
        f"{RUSSIAN_WEEKDAYS[value.weekday()]}"
    )


def format_schedule(
    lessons: Sequence[Lesson],
    target: str,
    lesson_date: date,
    role: str,
) -> str:
    ordered_lessons = _ordered_lessons(lessons)
    lines = [
        _target_heading(target, role, icon="📚"),
        html.escape(format_date(lesson_date)),
    ]

    if not ordered_lessons:
        lines.extend(
            ["", "На этот день занятий нет или расписание ещё не опубликовано."]
        )
        return "\n".join(lines)

    lines.extend([_summary(ordered_lessons, role), ""])
    lines.extend(_lesson_blocks(ordered_lessons, role))
    return "\n".join(lines).rstrip()


def format_week(
    schedule_by_date: Mapping[date, Sequence[Lesson]],
    target: str,
    role: str,
) -> str:
    lines = [
        "📅 <b>Расписание на неделю</b>",
        _target_heading(target, role),
    ]
    if not schedule_by_date:
        lines.extend(["", "Расписание на эту неделю ещё не опубликовано."])
        return "\n".join(lines)

    for lesson_date in sorted(schedule_by_date):
        lessons = _ordered_lessons(schedule_by_date[lesson_date])
        lines.extend(["", f"<b>{html.escape(format_date(lesson_date))}</b>"])
        if not lessons:
            lines.append("Занятий нет.")
            continue
        lines.append(_summary(lessons, role))
        lines.append("")
        lines.extend(_lesson_blocks(lessons, role))

    return "\n".join(lines).rstrip()


def format_changes(changes: Sequence[ScheduleChange]) -> str:
    if not changes:
        return "🔄 <b>Изменений в расписании нет</b>"

    by_date: dict[date, list[ScheduleChange]] = defaultdict(list)
    for change in changes:
        by_date[change.lesson_date].append(change)

    lines = ["🔄 <b>Расписание изменилось</b>"]
    for lesson_date in sorted(by_date):
        lines.extend(["", f"<b>{html.escape(format_date(lesson_date))}</b>"])
        date_changes = sorted(by_date[lesson_date], key=_change_sort_key)
        for index, change in enumerate(date_changes):
            if index:
                lines.append("")
            if change.kind == "added" and change.after is not None:
                lines.extend(_format_added_or_removed(change.after, added=True))
            elif change.kind == "removed" and change.before is not None:
                lines.extend(_format_added_or_removed(change.before, added=False))
            elif (
                change.kind == "modified"
                and change.before is not None
                and change.after is not None
            ):
                lines.extend(_format_modified(change.before, change.after))

    return "\n".join(lines).rstrip()


def _target_heading(target: str, role: str, *, icon: str = "") -> str:
    label = "Группа" if role == "student" else "Преподаватель"
    prefix = f"{icon} " if icon else ""
    return f"{prefix}<b>{label} {html.escape(target)}</b>"


def _ordered_lessons(lessons: Sequence[Lesson]) -> list[Lesson]:
    return sorted(
        lessons,
        key=lambda lesson: (
            lesson.position,
            lesson.start_time or "",
            lesson.group_name.casefold(),
        ),
    )


def _summary(lessons: Sequence[Lesson], role: str) -> str:
    count = len(lessons)
    forms = (
        ("пара", "пары", "пар")
        if role == "student"
        else ("занятие", "занятия", "занятий")
    )
    count_label = f"{count} {_plural(count, forms)}"
    time_range = _overall_time_range(lessons)
    if time_range:
        return f"<b>{count_label}</b> • {html.escape(time_range)}"
    return f"<b>{count_label}</b>"


def _plural(number: int, forms: tuple[str, str, str]) -> str:
    remainder_100 = number % 100
    remainder_10 = number % 10
    if remainder_10 == 1 and remainder_100 != 11:
        return forms[0]
    if remainder_10 in (2, 3, 4) and remainder_100 not in (12, 13, 14):
        return forms[1]
    return forms[2]


def _overall_time_range(lessons: Sequence[Lesson]) -> str | None:
    starts = [lesson.start_time for lesson in lessons if lesson.start_time]
    ends = [lesson.end_time for lesson in lessons if lesson.end_time]
    if starts and ends:
        return f"{min(starts)}–{max(ends)}"
    if starts:
        return f"с {min(starts)}"
    if ends:
        return f"до {max(ends)}"
    return None


def _lesson_blocks(lessons: Sequence[Lesson], role: str) -> list[str]:
    lines: list[str] = []
    for index, lesson in enumerate(lessons):
        if index:
            lines.append("")
        prefix = NUMBER_EMOJI.get(lesson.lesson_number)
        if prefix is None:
            prefix = html.escape(lesson.lesson_label) if lesson.lesson_label else "•"
        subject = f"<b>{html.escape(lesson.subject or 'Предмет не указан')}</b>"
        if lesson.room:
            room = f"<b>{html.escape(lesson.room)}</b> ауд."
            heading = f"{room} | {subject}"
        else:
            heading = subject
        lines.append(f"{prefix} {heading}")
        lines.append(f"—{html.escape(_lesson_time(lesson))}—")

        secondary = _secondary_details(lesson, role)
        if secondary:
            lines.append(" • ".join(secondary))
    return lines


def _lesson_time(lesson: Lesson) -> str:
    if lesson.start_time and lesson.end_time:
        return f"{lesson.start_time}–{lesson.end_time}"
    if lesson.start_time:
        return f"с {lesson.start_time}"
    if lesson.end_time:
        return f"до {lesson.end_time}"
    return "Время не указано"


def _secondary_details(lesson: Lesson, role: str) -> list[str]:
    details: list[str] = []
    if role == "teacher":
        details.append(f"группа {html.escape(lesson.group_name)}")
    if role == "student" and lesson.teacher:
        details.append(html.escape(lesson.teacher))
    if lesson.subgroup:
        details.append(f"подгр. {html.escape(lesson.subgroup)}")
    return details


def _change_sort_key(change: ScheduleChange) -> tuple[int, str]:
    lesson = change.after or change.before
    if lesson is None:  # pragma: no cover - protected by ScheduleChange contract
        return (0, "")
    return lesson.position, lesson.group_name.casefold()


def _format_added_or_removed(lesson: Lesson, *, added: bool) -> list[str]:
    icon = "➕" if added else "➖"
    action = "Добавлено" if added else "Удалено"
    label = html.escape(lesson.lesson_label or "занятие")
    group = html.escape(lesson.group_name)
    lines = [
        f"{icon} <b>{action}: {label} · {group}</b>",
        f"<b>{html.escape(lesson.subject or 'Предмет не указан')}</b>",
        html.escape(_lesson_time(lesson)),
    ]
    details: list[str] = []
    if lesson.room:
        details.append(f"ауд. {html.escape(lesson.room)}")
    if lesson.teacher:
        details.append(html.escape(lesson.teacher))
    if lesson.subgroup:
        details.append(f"подгр. {html.escape(lesson.subgroup)}")
    if details:
        lines.append(" • ".join(details))
    return lines


def _format_modified(before: Lesson, after: Lesson) -> list[str]:
    lesson_label = after.lesson_label or before.lesson_label or "занятие"
    group_name = after.group_name or before.group_name
    lines = [
        "✏️ <b>Изменено: "
        f"{html.escape(lesson_label)} · {html.escape(group_name)}</b>"
    ]

    differences = (
        ("Предмет", before.subject, after.subject),
        ("Время", _lesson_time(before), _lesson_time(after)),
        ("Преподаватель", before.teacher, after.teacher),
        ("Аудитория", before.room, after.room),
        ("Подгруппа", before.subgroup, after.subgroup),
    )
    for label, old_value, new_value in differences:
        if old_value != new_value:
            lines.append(
                f"• {label}: {_change_value(old_value)} → {_change_value(new_value)}"
            )

    if before.lesson_label != after.lesson_label:
        lines.append(
            "• Номер пары: "
            f"{_change_value(before.lesson_label)} → "
            f"{_change_value(after.lesson_label)}"
        )
    if len(lines) == 1:
        lines.append("• Данные занятия обновлены.")
    return lines


def _change_value(value: str | None) -> str:
    return html.escape(value) if value else "—"
