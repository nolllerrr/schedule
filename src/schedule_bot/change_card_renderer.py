"""Render complete before/after notification snapshots with the schedule typography."""
from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from io import BytesIO

from PIL import Image

from schedule_bot.card_renderer import (
    ACCENT, MUTED, WHITE, CardNotSupported, _Canvas, _field, _time,
)
from schedule_bot.domain import Lesson, ScheduleChange
from schedule_bot.presentation import RUSSIAN_MONTHS, RUSSIAN_WEEKDAYS


TEMPLATE_VERSION = "change-cards-v1.1"
MAX_CHANGES = 6
STYLES = {
    "modified": ("Пара изменилась", "#E7C586", "#353225"),
    "added": ("Добавлена пара", "#83DFB5", "#18372F"),
    "removed": ("Пара убрана", "#EAA6A5", "#3A282D"),
    "moved": ("Перенос пары", "#9BC9EF", "#203346"),
}


def _text(c: _Canvas, x: int, y: int, value: str, *, size: int = 30,
          color: str = WHITE, bold: bool = False, lines: int = 1) -> None:
    wrapped, _, fitted = c.typeface.fit(
        value, 408, sizes=range(size, 23, -1), bold=bold,
        lines=lines, max_height=lines * (size + 4),
    )
    for index, line in enumerate(wrapped):
        c.text(x, y + index * (fitted + 4), line, fitted, color, bold)


def _panel(c: _Canvas, left: int, top: int, lesson: Lesson | None, *,
           label: str, color: str, tint: str, show_position: bool) -> None:
    c.box((left, top, left + 472, top + 480), 24, tint)
    x = left + 32
    _text(c, x, top + 28, label, size=26, color=color, bold=True)
    if lesson is None:
        _text(c, x, top + 176, "Нет занятия", size=36, color=MUTED)
        return
    parts = lesson.effective_segments()
    if len(parts) != 1:
        raise CardNotSupported("Параллельные подгруппы доступны полным текстом.")
    part = parts[0]
    heading = _field(lesson.lesson_label) or "Занятие"
    if lesson.lesson_number is not None and str(lesson.lesson_number) not in heading:
        heading += f" · пара {lesson.lesson_number}"
    if show_position:
        heading += f" · позиция {lesson.position}"
    _text(c, x, top + 72, heading, size=28, color=color, lines=2)
    _text(c, x, top + 144, _field(part.subject) or "Предмет не указан",
          size=36, bold=True, lines=3)
    _text(c, x, top + 264, _field(part.teacher) or "Преподаватель не указан",
          size=28, color=MUTED, lines=2)
    _text(c, x, top + 332, "Аудитория: " + (_field(part.room) or "не указана"),
          size=28, lines=2)
    start, end = _time(lesson.start_time), _time(lesson.end_time)
    time = f"{start}–{end}" if start and end else (
        f"с {start}" if start else f"до {end}" if end else "Время не указано"
    )
    _text(c, x, top + 402, time, size=32, color=color, bold=True)
    if part.subgroup:
        _text(c, x, top + 444, "Подгруппа: " + (_field(part.subgroup) or "не указана"), size=24)


def render_changes(changes: Sequence[ScheduleChange], *, role: str, target: str,
                   lesson_date: date) -> bytes:
    if role not in {"student", "teacher"} or not 0 < len(changes) <= MAX_CHANGES:
        raise CardNotSupported("Слишком много изменений для одной карточки.")
    target = _field(target) or ""
    size = (1080, 336 + 604 * len(changes))
    c = _Canvas(size)
    c.text(56, 52, "schedule", 43, bold=True)
    c.fitted_text(56, 132, "Расписание изменилось", 968,
                  sizes=range(64, 45, -1), bold=True)
    c.text(56, 220, f"{lesson_date.day} {RUSSIAN_MONTHS[lesson_date.month]} · "
           f"{RUSSIAN_WEEKDAYS[lesson_date.weekday()]}", 30, ACCENT)
    c.fitted_text(56, 266, target, 968, sizes=range(32, 23, -1), color=MUTED)
    for index, change in enumerate(changes):
        if change.kind not in STYLES:
            raise CardNotSupported("Неизвестный вид изменения.")
        if ((change.kind == "added" and (change.before is not None or change.after is None))
                or (change.kind == "removed" and (change.before is None or change.after is not None))
                or (change.kind in {"modified", "moved"} and (change.before is None or change.after is None))):
            raise CardNotSupported("Неполный снимок изменения.")
        for lesson in (change.before, change.after):
            if lesson is not None and (lesson.lesson_date != lesson_date
                                       or (role == "student" and lesson.group_name != target)):
                raise CardNotSupported("Изменение относится к другому дню или группе.")
        top = 324 + 604 * index
        title, color, tint = STYLES[change.kind]
        group = _field(change.group_name) or ""
        c.box((56, top, 1024, top + 72), 22, tint)
        c.fitted_text(88, top + 22, f"{title} · {group}", 904,
                      sizes=range(34, 23, -1), color=color, bold=True)
        show_position = (change.before is not None and change.after is not None
                         and change.before.position != change.after.position
                         and change.before.lesson_label == change.after.lesson_label)
        _panel(c, 56, top + 88, change.before, label="БЫЛО", color=MUTED,
               tint="#14272D", show_position=show_position)
        _panel(c, 552, top + 88, change.after, label="СТАЛО", color=color,
               tint=tint, show_position=show_position)
    output = BytesIO()
    c.image.resize(size, Image.Resampling.LANCZOS).save(output, format="PNG")
    return output.getvalue()
