from dataclasses import replace
from datetime import date
from io import BytesIO

from PIL import Image
import pytest

from schedule_bot.card_renderer import (
    CardNotSupported, IMAGE_SIZE, SLOT_COUNT, normalize_schedule, render_card,
    _Typography,
)
from schedule_bot.domain import Lesson, LessonSegment

DAY = date(2026, 10, 26)


def lesson(number: int, **changes) -> Lesson:
    value = Lesson(DAY, "ИС-22", number + 10, f"{number} пара", number,
                   "10:40", "12:10", "Математика", "Иванов И. И.", "204")
    return replace(value, **changes)


def model(lessons):
    return normalize_schedule(lessons, role="student", target="ИС-22", lesson_date=DAY)


def test_pair_numbers_preserve_absent_first_and_last_slots():
    card = model([lesson(2), lesson(3)])
    assert len(card.slots) == SLOT_COUNT
    assert card.slots[0].entry is None
    assert card.slots[1].entry.segments[0].subject == "Математика"
    assert card.slots[2].entry is not None
    assert all(slot.entry is None for slot in card.slots[3:])
    assert card.lesson_count == 2
    assert card.window_count == 0


def test_only_internal_missing_pair_is_window():
    card = model([lesson(4), lesson(2)])
    assert card.window_count == 1
    assert card.slots[2].entry is None
    assert card.slots[3].number == 4


def test_sparse_and_dense_images_have_identical_size_and_slot_positions():
    sparse = model([lesson(2), lesson(4)])
    dense = model([lesson(number) for number in range(1, SLOT_COUNT + 1)])
    assert [slot.bounds for slot in sparse.slots] == [slot.bounds for slot in dense.slots]
    for card in (sparse, dense):
        with Image.open(BytesIO(render_card(card))) as image:
            assert image.size == IMAGE_SIZE
            assert image.format == "PNG"


def test_subgroups_preserve_linked_assignments():
    parts = (LessonSegment("Английский", "Иванова А. А.", "201", "1"),
             LessonSegment("Немецкий", "Петров Б. Б.", "305", "2"))
    card = model([lesson(2, segments=parts)])
    assert card.slots[1].entry.segments == parts
    assert render_card(card).startswith(b"\x89PNG")


def test_cyrillic_long_subject_and_missing_values_render():
    card = model([lesson(2, subject="МДК.01.01. Разработка программных модулей",
                         teacher=None, room=None, start_time=None, end_time=None)])
    assert card.time_range is None
    assert card.slots[1].entry.time_label == "время не указано"
    assert render_card(card).startswith(b"\x89PNG")


def test_partial_time_does_not_invent_overall_interval():
    card = model([lesson(2), lesson(3, start_time=None)])
    assert card.time_range is None
    assert card.slots[2].entry.time_label == "до 12:10"


def test_reversed_cross_pair_chronology_uses_text():
    with pytest.raises(CardNotSupported):
        model([lesson(1, start_time="13:00", end_time="14:00"),
               lesson(2, start_time="09:00", end_time="10:00")])


def test_interval_uses_first_last_pair_not_class_hour():
    card = model([lesson(2, start_time="10:40", end_time="12:10"),
                  lesson(4, start_time="14:20", end_time="15:50"),
                  lesson(1, lesson_number=None, lesson_label="кл.час", subject="Классный час",
                         start_time="08:00", end_time="08:30")])
    assert card.lesson_count == 2
    assert card.window_count == 1
    assert card.time_range == "10:40–15:50"
    assert card.class_hour is not None
    assert card.slots[0].entry is None
    assert render_card(card).startswith(b"\x89PNG")


@pytest.mark.parametrize("number", [0, 7, 8, 10, None])
def test_unsupported_numbers_fall_back_instead_of_hiding_pairs(number):
    with pytest.raises(CardNotSupported):
        model([lesson(2, lesson_number=number)])


@pytest.mark.parametrize("changes", [
    {"subject": "Я" * 321}, {"subject": "Китайский 中文"},
    {"start_time": "99:99"}, {"end_time": "09:00"},
    {"lesson_label": "2/3 пара"}, {"group_name": "Другая группа"},
    {"lesson_date": date(2026, 10, 27)},
])
def test_malformed_or_unrepresentable_values_use_text(changes):
    with pytest.raises(CardNotSupported):
        model([lesson(2, **changes)])


def test_overflow_uses_text_without_truncating_important_details():
    card = model([lesson(2, subject="Разработка " * 25)])
    with pytest.raises(CardNotSupported):
        render_card(card)


def test_empty_duplicate_and_teacher_schedules_use_text():
    with pytest.raises(CardNotSupported):
        model([])
    with pytest.raises(CardNotSupported):
        model([lesson(2), lesson(2)])
    with pytest.raises(CardNotSupported):
        normalize_schedule([lesson(2)], role="teacher", target="Иванов", lesson_date=DAY)


def test_more_than_two_subgroups_use_text():
    parts = tuple(LessonSegment("Язык", f"Преподаватель {index}", str(index), str(index)) for index in range(3))
    with pytest.raises(CardNotSupported):
        model([lesson(2, segments=parts)])


def test_tall_glyphs_and_negative_bearings_are_measured_as_ink():
    typography = _Typography()
    lines, font, size = typography.fit("⸎", 100, sizes=range(30, 16, -1), max_height=24)
    left, top, right, bottom = font.getbbox(lines[0], anchor="lt")
    assert bottom - top <= 24
    assert size < 30
    assert right - left <= 100
    with pytest.raises(CardNotSupported):
        typography.fit("⸎", 100, sizes=range(30, 28, -1), max_height=24)
