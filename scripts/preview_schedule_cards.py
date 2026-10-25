"""Generate synthetic design previews, never using the live database.

Run: python scripts/preview_schedule_cards.py [--output docs/examples/schedule-cards]
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import date
from pathlib import Path
import sys
from time import perf_counter

# Also work in a checkout before installing the project.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from schedule_bot.card_renderer import normalize_schedule, render_card  # noqa: E402
from schedule_bot.domain import Lesson  # noqa: E402


APPROVED_DAY = date(2026, 10, 28)
DEMO_GROUP = "ИС-22"


def approved_demo_lessons() -> list[Lesson]:
    """Reproduce the supplied reference using domain objects, not renderer data."""
    rows = (
        ("Математика", "24", "Иванов И. И.", "09:00", "10:30"),
        ("Базы данных", "31", "Петров П. П.", "10:40", "12:10"),
        ("Системное программирование", "21", "Сидоров А. А.", "12:20", "13:50"),
        ("Иностранный язык", "18", "Смирнова Е. А.", "14:10", "15:40"),
        ("Физическая культура", "спортзал", "Кузнецов Д. В.", "15:50", "17:20"),
    )
    return [
        Lesson(APPROVED_DAY, DEMO_GROUP, number, f"{number} пара", number,
               start, end, subject, teacher, room)
        for number, (subject, room, teacher, start, end) in enumerate(rows, start=1)
    ]


def short_demo_lessons() -> list[Lesson]:
    """Keep two real slot numbers and the empty slots between them."""
    return [lesson for lesson in approved_demo_lessons() if lesson.lesson_number in (2, 5)]


def long_fields_demo_lessons() -> list[Lesson]:
    """Exercise measured title and metadata layout with fictional long fields."""
    lessons = short_demo_lessons()
    lessons[0] = replace(
        lessons[0], subject="МДК.01.01. Разработка программных модулей",
        teacher="Александрова-Кузнецова А. В.", room="ауд. 24а",
    )
    return lessons


def demo_lessons(numbers: tuple[int, ...]) -> list[Lesson]:
    """All names, rooms and bell times in these previews are fictional."""
    subjects = (
        ("Математика", "Соколова А. В.", "204"),
        ("МДК.01.01. Разработка программных модулей", "Орлов Д. С.", "312"),
        ("Иностранный язык", "Лебедева М. И.", "208"),
        ("Основы алгоритмизации и программирования", "Волков А. Н.", "305"),
        ("Физическая культура", "Морозова Е. А.", "спортзал"),
        ("Информационные технологии", "Крылов П. В.", "302"),
    )
    times = (
        ("09:00", "10:30"), ("10:40", "12:10"), ("12:40", "14:10"),
        ("14:20", "15:50"), ("16:00", "17:30"), ("17:40", "19:10"),
    )
    return [
        Lesson(date(2026, 10, 26), DEMO_GROUP, number + 1, f"{number} пара", number,
               *times[number - 1], *subjects[number - 1])
        for number in numbers
    ]


def class_hour_demo_lessons() -> list[Lesson]:
    lessons = [
        replace(lesson, position=lesson.lesson_number + (lesson.lesson_number > 2))
        for lesson in demo_lessons((1, 2, 3, 4))
    ]
    lessons.append(Lesson(
        date(2026, 10, 26), DEMO_GROUP, 3, "кл.час", None, "12:15", "12:35",
        "Классный час", "Соколова А. В.", "204",
    ))
    return lessons


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("docs/examples/schedule-cards"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    for name, lessons in (
        ("approved-day", approved_demo_lessons()),
        ("short-day", short_demo_lessons()),
        ("long-fields", long_fields_demo_lessons()),
        ("second-pair", demo_lessons((2, 3))),
        ("gap-between-pairs", demo_lessons((2, 4))),
        ("dense-day", demo_lessons((1, 2, 3, 4, 5, 6))),
        ("class-hour", class_hour_demo_lessons()),
    ):
        started = perf_counter()
        model = normalize_schedule(
            lessons, role="student", target=DEMO_GROUP, lesson_date=lessons[0].lesson_date,
        )
        result = render_card(model)
        elapsed = perf_counter() - started
        destination = args.output / f"{name}.png"
        destination.write_bytes(result)
        print(f"{destination}: {model.image_size[0]}x{model.image_size[1]}, {len(result):,} bytes, {elapsed * 1000:.1f} ms, {model.lesson_count} pairs, {model.window_count} gaps")


if __name__ == "__main__":
    main()
