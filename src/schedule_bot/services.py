from __future__ import annotations

import hashlib
import html
from collections import defaultdict
from datetime import date
from pathlib import Path

from schedule_bot.domain import ImportResult, Lesson, ScheduleChange
from schedule_bot.downloader import ScheduleDownloader
from schedule_bot.parser import ExcelScheduleParser
from schedule_bot.repository import ScheduleRepository


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


class ScheduleUpdater:
    def __init__(
        self,
        downloader: ScheduleDownloader,
        parser: ExcelScheduleParser,
        repository: ScheduleRepository,
    ) -> None:
        self.downloader = downloader
        self.parser = parser
        self.repository = repository

    async def update_once(self) -> ImportResult:
        downloaded = await self.downloader.fetch_latest()
        parsed = self.parser.parse(
            downloaded.path, source_filename=downloaded.link.filename
        )
        return self.repository.import_schedule(
            parsed,
            sha256=downloaded.sha256,
            source_url=downloaded.link.url,
        )

    def import_local(self, path: str | Path) -> ImportResult:
        source_path = Path(path)
        digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
        parsed = self.parser.parse(source_path)
        return self.repository.import_schedule(parsed, sha256=digest)


def format_date(value: date) -> str:
    return (
        f"{value.day} {RUSSIAN_MONTHS[value.month]}, "
        f"{RUSSIAN_WEEKDAYS[value.weekday()]}"
    )


def format_schedule(
    lessons: list[Lesson],
    *,
    target: str,
    lesson_date: date,
    role: str,
) -> str:
    title = "группы" if role == "student" else "преподавателя"
    lines = [
        f"<b>Расписание {title} {html.escape(target)}</b>",
        html.escape(format_date(lesson_date)),
        "",
    ]
    if not lessons:
        lines.append("Занятий нет или расписание ещё не опубликовано.")
        return "\n".join(lines)

    for lesson in lessons:
        time_label = (
            f"{lesson.start_time}–{lesson.end_time}"
            if lesson.start_time and lesson.end_time
            else "время не указано"
        )
        group_suffix = (
            f" · {html.escape(lesson.group_name)}" if role == "teacher" else ""
        )
        lines.append(
            f"<b>{html.escape(lesson.lesson_label)} · {time_label}{group_suffix}</b>"
        )
        lines.append(html.escape(lesson.subject))
        if role == "student" and lesson.teacher:
            lines.append(f"Преподаватель: {html.escape(lesson.teacher)}")
        if lesson.room:
            lines.append(f"Аудитория: {html.escape(lesson.room)}")
        lines.append("")
    return "\n".join(lines).rstrip()


def changes_for_profile(
    changes: tuple[ScheduleChange, ...], *, role: str, target: str
) -> tuple[ScheduleChange, ...]:
    needle = target.casefold()
    result: list[ScheduleChange] = []
    for change in changes:
        lessons = tuple(item for item in (change.before, change.after) if item)
        if role == "student" and any(item.group_name == target for item in lessons):
            result.append(change)
        elif role == "teacher" and any(
            item.teacher and needle in item.teacher.casefold() for item in lessons
        ):
            result.append(change)
    return tuple(result)


def format_changes(changes: tuple[ScheduleChange, ...]) -> str:
    by_date: dict[date, list[ScheduleChange]] = defaultdict(list)
    for change in changes:
        by_date[change.lesson_date].append(change)
    lines = ["<b>Расписание обновлено</b>", ""]
    labels = {"added": "Добавлено", "removed": "Удалено", "modified": "Изменено"}
    for lesson_date in sorted(by_date):
        lines.append(f"<b>{html.escape(format_date(lesson_date))}</b>")
        for change in by_date[lesson_date]:
            lesson = change.after or change.before
            if lesson is None:
                continue
            lines.append(
                f"{labels[change.kind]}: {html.escape(lesson.lesson_label)}, "
                f"{html.escape(lesson.group_name)} — {html.escape(lesson.subject)}"
            )
        lines.append("")
    return "\n".join(lines).rstrip()

