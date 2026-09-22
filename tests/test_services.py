from datetime import date

from schedule_bot.domain import Lesson
from schedule_bot.notifier import _split_message
from schedule_bot.services import format_schedule


def test_schedule_formatter_escapes_html() -> None:
    lesson = Lesson(
        lesson_date=date(2026, 9, 22),
        group_name="ПД-12",
        position=1,
        lesson_label="1 пара",
        lesson_number=1,
        start_time="09:00",
        end_time="10:30",
        subject="Право <основы>",
        teacher="Иванова А.А.",
        room="43",
    )

    result = format_schedule(
        [lesson], target="ПД-12", lesson_date=lesson.lesson_date, role="student"
    )

    assert "Право &lt;основы&gt;" in result
    assert "09:00–10:30" in result


def test_long_notification_is_split() -> None:
    chunks = _split_message(("абв" * 1000 + "\n\n") * 3, limit=1000)
    assert len(chunks) > 1
    assert all(len(chunk) <= 1000 for chunk in chunks)

