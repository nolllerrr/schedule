"""Preflight checks performed before activating an imported schedule."""

from __future__ import annotations

from collections import Counter
from datetime import date

from schedule_bot.domain import Lesson, ParsedSchedule


class ImportQuarantinedError(ValueError):
    def __init__(self, reasons: tuple[str, ...]) -> None:
        self.reasons = reasons
        super().__init__("Импорт помещён в карантин: " + "; ".join(reasons))


def validate_import(
    parsed: ParsedSchedule,
    old_lessons: list[Lesson],
    previous_groups: dict[date, set[str]],
    previous_dates: set[date],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return structural errors and anomalies requiring an explicit review."""
    errors = list(parsed.validation_errors)
    anomalies: list[str] = []
    dates = set(parsed.dates)
    if parsed.start_date > parsed.end_date or not dates:
        errors.append("Неверный диапазон или пустой набор дат")
    if len(dates) != len(parsed.dates):
        errors.append("Повторяются даты публикации")
    if any(not parsed.start_date <= day <= parsed.end_date for day in dates):
        errors.append("Дата публикации вне диапазона файла")
    if parsed.date_groups and set(parsed.date_groups) != dates:
        errors.append("Охват заголовков групп не соответствует датам публикации")
    if not set(parsed.confirmed_empty_dates) <= dates:
        errors.append("Подтверждённый пустой день вне публикации")
    keys = [
        (lesson.lesson_date, lesson.group_name, lesson.position)
        for lesson in parsed.lessons
    ]
    if len(keys) != len(set(keys)):
        errors.append("Повторяются ключи занятий")
    for lesson in parsed.lessons:
        if (
            lesson.lesson_date not in dates
            or not lesson.subject.strip()
            or lesson.position < 1
        ):
            errors.append("Некорректное занятие или занятие вне опубликованных дат")
        if parsed.date_groups and lesson.group_name not in parsed.date_groups.get(
            lesson.lesson_date, ()
        ):
            errors.append("Занятие не принадлежит распознанному заголовку группы")
        if lesson.lesson_date in parsed.confirmed_empty_dates:
            errors.append("Подтверждение пустого дня противоречит занятиям")
        if lesson.segments:
            subgroups = [part.subgroup for part in lesson.segments if part.subgroup]
            if any(not part.subject.strip() for part in lesson.segments):
                errors.append("У части занятия отсутствует предмет")
            if len(subgroups) != len(set(subgroups)):
                errors.append("Повторяются подгруппы внутри занятия")
            if len(lesson.segments) > 1 and any(
                part.subgroup not in {"1", "2"} for part in lesson.segments
            ):
                errors.append("Части параллельного занятия требуют явных подгрупп 1/2")

    missing_dates = {
        day
        for day in previous_dates
        if parsed.start_date <= day <= parsed.end_date and day not in dates
    }
    if missing_dates:
        anomalies.append(
            "Пропущены ранее опубликованные даты: "
            + ", ".join(map(str, sorted(missing_dates)))
        )
    old_counts = Counter((lesson.lesson_date, lesson.group_name) for lesson in old_lessons)
    new_counts = Counter((lesson.lesson_date, lesson.group_name) for lesson in parsed.lessons)
    book_groups = {group for groups in parsed.date_groups.values() for group in groups}
    for day in sorted(dates):
        actual_groups = set(parsed.date_groups.get(day, ())) or {
            lesson.group_name for lesson in parsed.lessons if lesson.lesson_date == day
        }
        expected_groups = previous_groups.get(day, set()) | book_groups
        missing = expected_groups - actual_groups
        if missing:
            anomalies.append(f"{day}: пропущены группы " + ", ".join(sorted(missing)))
        old_total = sum(
            count for (lesson_day, _), count in old_counts.items() if lesson_day == day
        )
        new_total = sum(
            count for (lesson_day, _), count in new_counts.items() if lesson_day == day
        )
        if day in parsed.confirmed_empty_dates:
            continue
        if old_total and not new_total:
            anomalies.append(f"{day}: день стал пустым без подтверждения ЗАНЯТИЙ НЕТ")
        elif old_total >= 4 and new_total <= old_total / 2:
            anomalies.append(
                f"{day}: число занятий резко уменьшилось ({old_total} → {new_total})"
            )
        for (lesson_day, group), count in old_counts.items():
            if lesson_day != day:
                continue
            new_count = new_counts[(day, group)]
            if not new_count or (count >= 4 and new_count <= count / 2):
                anomalies.append(
                    f"{day}, {group}: число занятий уменьшилось ({count} → {new_count})"
                )
    return tuple(dict.fromkeys(errors)), tuple(dict.fromkeys(anomalies))
