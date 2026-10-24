"""Generate three synthetic design previews, never using the live database.

Run: python scripts/preview_schedule_cards.py [--output docs/examples/schedule-cards]
"""
from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import sys
from time import perf_counter

# Also work in a checkout before installing the project.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from schedule_bot.card_renderer import IMAGE_SIZE, normalize_schedule, render_card  # noqa: E402
from schedule_bot.domain import Lesson  # noqa: E402


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
        Lesson(date(2026, 10, 26), "ИС-22", number + 1, f"{number} пара", number,
               *times[number - 1], *subjects[number - 1])
        for number in numbers
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("docs/examples/schedule-cards"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    for name, numbers in (
        ("second-pair", (2, 3)),
        ("gap-between-pairs", (2, 4)),
        ("dense-day", (1, 2, 3, 4, 5, 6)),
    ):
        started = perf_counter()
        model = normalize_schedule(demo_lessons(numbers), role="student", target="ИС-22", lesson_date=date(2026, 10, 26))
        result = render_card(model)
        elapsed = perf_counter() - started
        destination = args.output / f"{name}.png"
        destination.write_bytes(result)
        print(f"{destination}: {IMAGE_SIZE[0]}x{IMAGE_SIZE[1]}, {len(result):,} bytes, {elapsed * 1000:.1f} ms, {model.lesson_count} pairs, {model.window_count} gaps")


if __name__ == "__main__":
    main()
