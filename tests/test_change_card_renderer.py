from dataclasses import replace
from datetime import date
from io import BytesIO

import pytest
from PIL import Image

from schedule_bot.card_renderer import CardNotSupported, _Canvas
from schedule_bot.change_card_renderer import MAX_CHANGES, render_changes
from schedule_bot.domain import Lesson, LessonSegment, ScheduleChange


DAY = date(2026, 10, 26)
BEFORE = Lesson(DAY, "ИС-22", 2, "2 пара", 2, "10:30", "11:40",
                "Математика", "Иванов И.И.", "24", "1")
AFTER = replace(BEFORE, position=3, lesson_label="3 пара", lesson_number=3,
                start_time="11:50", end_time="13:00", subject="Физика",
                teacher="Петров П.П.", room="45", subgroup="2")


def render(changes, **profile):
    return render_changes(changes, role=profile.get("role", "student"),
                          target=profile.get("target", "ИС-22"), lesson_date=DAY)


def test_before_after_card_preserves_all_assignment_details(monkeypatch):
    drawn = []
    original = _Canvas.text

    def record(canvas, x, y, value, *args, **kwargs):
        drawn.append(value)
        return original(canvas, x, y, value, *args, **kwargs)

    monkeypatch.setattr(_Canvas, "text", record)
    with Image.open(BytesIO(render([ScheduleChange("modified", BEFORE, AFTER)]))) as image:
        assert image.mode == "RGB" and image.size == (1080, 940)
    text = "\n".join(drawn)
    for field in ("БЫЛО", "СТАЛО", "2 пара", "3 пара", "Математика", "Физика",
                  "Иванов И.И.", "Петров П.П.", "24", "45", "10:30–11:40", "11:50–13:00",
                  "Подгруппа: 1", "Подгруппа: 2", "ИС-22", "26 октября"):
        assert field in text


def test_multiple_changes_and_teachers_keep_all_groups(monkeypatch):
    drawn = []
    original = _Canvas.text

    def record(canvas, x, y, value, *args, **kwargs):
        drawn.append(value)
        return original(canvas, x, y, value, *args, **kwargs)

    monkeypatch.setattr(_Canvas, "text", record)
    changes = [ScheduleChange("removed", BEFORE, None),
               ScheduleChange("added", None, replace(AFTER, group_name="ПД-22"))]
    with Image.open(BytesIO(render(changes, role="teacher", target="Иванов И.И."))) as image:
        assert image.size == (1080, 1544)
    assert any("ПД-22" in value for value in drawn)
    assert any("ИС-22" in value for value in drawn)
    assert drawn.count("Нет занятия") == 2


@pytest.mark.parametrize("change", [
    ScheduleChange("modified", BEFORE, replace(AFTER, subject="x" * 400)),
    ScheduleChange("modified", BEFORE, replace(AFTER, segments=(
        LessonSegment("Физика", subgroup="1"), LessonSegment("Химия", subgroup="2")))),
    ScheduleChange("moved", BEFORE, replace(AFTER, lesson_date=date(2026, 10, 27))),
    ScheduleChange("modified", None, AFTER),
])
def test_unsupported_data_falls_back_instead_of_losing_fields(change):
    with pytest.raises(CardNotSupported):
        render([change])


def test_excessive_changes_are_rejected_before_allocating_an_image():
    with pytest.raises(CardNotSupported):
        render([ScheduleChange("modified", BEFORE, AFTER)] * (MAX_CHANGES + 1))
