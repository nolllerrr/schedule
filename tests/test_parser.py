from datetime import date
from pathlib import Path

from schedule_bot.parser.excel import (
    ExcelScheduleParser,
    normalize_group,
    parse_filename_date_range,
)


FIXTURE = Path(__file__).parents[1] / "21.-23.09.2026.xlsx"


def test_compact_filename_date_range() -> None:
    assert parse_filename_date_range("21.-23.09.2026.xlsx") == (
        date(2026, 9, 21),
        date(2026, 9, 23),
    )


def test_full_filename_date_range() -> None:
    assert parse_filename_date_range("14.09.26-18.09.26.xlsx") == (
        date(2026, 9, 14),
        date(2026, 9, 18),
    )


def test_group_normalization() -> None:
    assert normalize_group(" Т -21 ") == "Т-21"


def test_real_workbook_is_limited_by_filename_range() -> None:
    parsed = ExcelScheduleParser().parse(FIXTURE)

    assert parsed.dates == (
        date(2026, 9, 21),
        date(2026, 9, 22),
        date(2026, 9, 23),
    )
    assert len(parsed.groups) == 28
    assert "Т-21" in parsed.groups
    assert all(parsed.start_date <= lesson.lesson_date <= parsed.end_date for lesson in parsed.lessons)


def test_real_workbook_first_lesson_for_pd12() -> None:
    parsed = ExcelScheduleParser().parse(FIXTURE)
    lesson = next(
        item
        for item in parsed.lessons
        if item.lesson_date == date(2026, 9, 22)
        and item.group_name == "ПД-12"
        and item.lesson_number == 1
    )

    assert lesson.start_time == "09:00"
    assert lesson.end_time == "10:30"
    assert lesson.subject == "Теория гос. и права"
    assert lesson.teacher == "Булаева В.В."
    assert lesson.room == "43"


def test_hidden_template_data_outside_matching_day_is_ignored() -> None:
    parsed = ExcelScheduleParser().parse(FIXTURE)

    monday_pd12 = [
        item
        for item in parsed.lessons
        if item.lesson_date == date(2026, 9, 21) and item.group_name == "ПД-12"
    ]
    assert monday_pd12
    assert monday_pd12[0].subject.startswith("МДК.01.03")
    assert all("Экономика организ." not in item.subject for item in monday_pd12)

