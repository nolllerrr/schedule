from datetime import date
from pathlib import Path

from schedule_bot.parser import ExcelScheduleParser
from schedule_bot.repository import ScheduleRepository


def test_import_is_idempotent_and_queryable(
    tmp_path: Path, schedule_workbook: Path
) -> None:
    repository = ScheduleRepository(tmp_path / "schedule.db")
    parsed = ExcelScheduleParser().parse(schedule_workbook)

    first = repository.import_schedule(parsed, sha256="first")
    second = repository.import_schedule(parsed, sha256="first")

    assert first.skipped is False
    assert first.changes
    assert second.skipped is True
    lessons = repository.lessons_for(
        role="student", target="ПД-12", lesson_date=date(2026, 9, 22)
    )
    assert lessons[0].subject == "Теория гос. и права"
    assert repository.available_dates(from_date=date(2026, 9, 22)) == [
        date(2026, 9, 22),
        date(2026, 9, 23),
    ]


def test_profile_round_trip(tmp_path: Path) -> None:
    repository = ScheduleRepository(tmp_path / "schedule.db")
    repository.save_profile(123, role="student", target="ПД-12")

    assert repository.get_profile(123) == {
        "user_id": 123,
        "role": "student",
        "target": "ПД-12",
        "notifications": True,
    }
    repository.set_notifications(123, False)
    assert repository.get_profile(123)["notifications"] is False


def test_teacher_query_is_unicode_case_insensitive(
    tmp_path: Path, schedule_workbook: Path
) -> None:
    repository = ScheduleRepository(tmp_path / "schedule.db")
    parsed = ExcelScheduleParser().parse(schedule_workbook)
    repository.import_schedule(parsed, sha256="teacher-search")

    lessons = repository.lessons_for(
        role="teacher",
        target="яцишина н.в.",
        lesson_date=date(2026, 9, 22),
    )

    assert lessons
    assert all("Яцишина Н.В." in (lesson.teacher or "") for lesson in lessons)
