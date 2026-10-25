from dataclasses import replace
from datetime import date
from io import BytesIO

from PIL import Image
import pytest

from schedule_bot import card_renderer
from schedule_bot.card_renderer import (
    CardNotSupported, IMAGE_SIZE, SLOT_COUNT, normalize_schedule, render_card,
    _Typography, _room_parts, lesson_count_label,
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
    assert len(card.slots) == SLOT_COUNT == 5
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


def test_separated_assignments_keep_their_slots_and_real_statistics():
    card = model([
        lesson(5, subject="Физическая культура", room="спортзал",
               start_time="15:50", end_time="17:20"),
        lesson(2, subject="Базы данных", room="31"),
    ])
    assert [slot.number for slot in card.slots if slot.entry] == [2, 5]
    assert card.slots[1].entry.segments[0].subject == "Базы данных"
    assert card.slots[4].entry.segments[0].room == "спортзал"
    assert card.lesson_count == 2
    assert card.time_range == "10:40–17:20"
    assert all(card.slots[index].entry is None for index in (0, 2, 3))


def test_sparse_and_dense_images_have_identical_size_and_slot_positions():
    sparse = model([lesson(2), lesson(4)])
    dense = model([lesson(number) for number in range(1, SLOT_COUNT + 1)])
    assert [slot.bounds for slot in sparse.slots] == [slot.bounds for slot in dense.slots]
    for card in (sparse, dense):
        with Image.open(BytesIO(render_card(card))) as image:
            assert image.size == IMAGE_SIZE == (1080, 1444)
            assert image.mode == "RGB"
            assert image.format == "PNG"


def test_subgroups_preserve_linked_assignments():
    parts = (LessonSegment("Английский", "Иванова А. А.", "201", "1"),
             LessonSegment("Немецкий", "Петров Б. Б.", "305", "2"))
    card = model([lesson(2, segments=parts)])
    assert card.slots[1].entry.segments == parts
    with pytest.raises(CardNotSupported):
        render_card(card)


@pytest.mark.parametrize("subject,subgroup,title", [
    ("Математика", "1", "Математика · подгр. 1"),
    ("ДОУ 2 п/гр", "2", "ДОУ 2 п/гр"),
    ("ДОУ 1 п / гр.", "1", "ДОУ 1 п / гр."),
    ("ДОУ 1 п/гр", "2", "ДОУ 1 п/гр · подгр. 2"),
])
def test_single_subgroup_renders_with_its_label(drawing, subject, subgroup, title):
    texts, _ = drawing
    card = model([lesson(2, subject=subject, subgroup=subgroup)])
    assert card.slots[1].entry.segments[0].subgroup == subgroup
    with Image.open(BytesIO(render_card(card))) as image:
        assert image.size == IMAGE_SIZE
    assert any(item["value"] == title for item in texts)
    assert any(item["value"] == "Иванов И. И." for item in texts)
    assert any(item["value"] == "204" for item in texts)
    assert any(item["value"] == "10:40–12:10" for item in texts)


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
    assert card.class_hour_before == 2
    assert render_card(card).startswith(b"\x89PNG")


@pytest.mark.parametrize("count,label", [
    (0, "0 пар"), (1, "1 пара"), (2, "2 пары"), (4, "4 пары"),
    (5, "5 пар"), (11, "11 пар"), (12, "12 пар"), (14, "14 пар"),
    (21, "21 пара"), (22, "22 пары"), (25, "25 пар"), (111, "111 пар"),
])
def test_lesson_count_uses_russian_plural_forms(count, label):
    assert lesson_count_label(count) == label


@pytest.mark.parametrize("room,parts", [
    (None, ("", "")), ("", ("", "")), ("24", ("24", " ауд.")),
    ("ауд. 24а", ("24а", " ауд.")),
    ("24а аудитория", ("24а", " ауд.")),
    ("спортзал", ("спортзал", "")),
    ("ауд. спортзал", ("спортзал", "")),
    ("спортзал аудитория", ("спортзал", "")),
    ("аудиозал", ("аудиозал", "")),
])
def test_room_notation_preserves_value_and_avoids_duplicate_suffix(room, parts):
    assert _room_parts(room) == parts


@pytest.fixture
def drawing(monkeypatch):
    texts = []
    boxes = []
    original_text = card_renderer._Canvas.text
    original_box = card_renderer._Canvas.box

    def text(canvas, x, y, value, size, color=card_renderer.WHITE, bold=False, anchor="lt"):
        left, top, right, bottom = canvas.typeface.font(size, bold).getbbox(value, anchor=anchor)
        texts.append({"value": value, "x": x, "y": y, "size": size,
                      "left": x + left / 2, "right": x + right / 2,
                      "bottom": y + (bottom - top) / 2,
                      "bold": bold, "anchor": anchor})
        return original_text(canvas, x, y, value, size, color, bold, anchor)

    def box(canvas, coords, radius, fill):
        boxes.append((coords, fill))
        return original_box(canvas, coords, radius, fill)

    monkeypatch.setattr(card_renderer._Canvas, "text", text)
    monkeypatch.setattr(card_renderer._Canvas, "box", box)
    return texts, boxes


def test_real_long_title_fits_above_room_and_teacher(drawing):
    texts, boxes = drawing
    subject = "Материалы и изд. сантех.устр. и сист. обесп.микроклим."
    card = model([lesson(3, subject=subject, teacher="Сафонов А.В.", room="59")])
    render_card(card)
    title_lines = [item for item in texts if item["bold"] and item["x"] == 162]
    assert len(title_lines) == 2
    assert " ".join(item["value"] for item in title_lines) == subject
    room_bounds, = [coords for coords, fill in boxes if fill == "#26454B"]
    assert max(item["bottom"] for item in title_lines) < room_bounds[1]
    assert all(item["left"] >= 162 and item["right"] <= 998 for item in title_lines)


def test_class_hour_for_single_subgroup_keeps_label_and_details(drawing):
    texts, _ = drawing
    card = model([lesson(1, lesson_number=None, lesson_label="кл.час",
                         subject="Классный час", subgroup="2")])
    render_card(card)
    assert any(item["value"] == "Классный час · подгр. 2" for item in texts)
    assert any(item["value"] == "Иванов И. И." for item in texts)


def test_class_hour_is_inserted_between_pairs_without_renumbering(drawing):
    texts, boxes = drawing
    lessons = [
        lesson(1, position=1, subject="Первая пара", start_time="09:00", end_time="10:10"),
        lesson(2, position=2, subject="Вторая пара", start_time="10:30", end_time="11:40"),
        lesson(3, position=3, lesson_number=None, lesson_label="кл.час", subject="Классный час",
               start_time="11:50", end_time="12:20", teacher="Соколова А. В.", room="104"),
        lesson(3, position=4, subject="Третья пара", start_time="13:00", end_time="14:00"),
    ]
    card = model(lessons[::-1])
    assert card.class_hour_before == 3
    assert card.lesson_count == 3
    assert card.time_range == "09:00–14:00"
    with Image.open(BytesIO(render_card(card))) as image:
        assert image.size == card.image_size == (1080, 1620)
    by_value = {item["value"]: item for item in texts}
    assert by_value["Вторая пара"]["y"] < by_value["Классный час"]["y"] < by_value["Третья пара"]["y"]
    assert [item["value"] for item in texts if item["x"] == 56 and item["value"].isdecimal()] == [
        "01", "02", "03", "04", "05",
    ]
    assert by_value["Соколова А. В."]["y"] == by_value["11:50–12:20"]["y"]
    activity_boxes = [coords for coords, _ in boxes if coords[0] == 136]
    assert len(activity_boxes) == 6
    assert "01–05" in by_value
    assert "06" not in by_value
    assert all(first[3] < second[1] for first, second in zip(activity_boxes, activity_boxes[1:]))
    assert activity_boxes[-1][3] < card.image_size[1]


@pytest.mark.parametrize("position,before", [(5, 2), (15, 3), (25, 4)])
def test_class_hour_position_uses_source_order_even_without_times(position, before):
    card = model([
        lesson(2, position=10, start_time=None, end_time=None),
        lesson(3, position=20, start_time=None, end_time=None),
        lesson(1, position=position, lesson_number=None, lesson_label="кл.час",
               subject="Классный час", start_time=None, end_time=None),
    ])
    assert card.class_hour_before == before
    assert render_card(card).startswith(b"\x89PNG")


def test_five_pairs_and_final_class_hour_all_fit(drawing):
    texts, _ = drawing
    card = model([
        *(lesson(number, position=number) for number in range(1, 6)),
        lesson(1, position=6, lesson_number=None, lesson_label="кл.час", subject="Классный час"),
    ])
    render_card(card)
    assert card.class_hour_before == 6
    assert card.lesson_count == 5
    hour, = [item for item in texts if item["value"] == "Классный час"]
    fifth_number, = [item for item in texts if item["value"] == "05"]
    assert hour["y"] > fifth_number["y"]
    assert card.class_hour_bounds[3] < card.image_size[1]


def test_day_with_only_class_hour_and_custom_subject_renders(drawing):
    texts, _ = drawing
    card = model([lesson(1, lesson_number=None, lesson_label="кл.час", subject="Разговор о профессии")])
    assert card.lesson_count == 0
    assert card.time_range is None
    render_card(card)
    assert any(item["value"] == "Классный час · Разговор о профессии" for item in texts)


@pytest.mark.parametrize("position", [10, 20, None])
def test_ambiguous_class_hour_position_uses_text(position):
    with pytest.raises(CardNotSupported):
        model([
            lesson(2, position=10), lesson(3, position=20),
            lesson(1, position=position, lesson_number=None, lesson_label="кл.час", subject="Классный час"),
        ])


def test_multiple_class_hours_use_text_instead_of_losing_an_activity():
    with pytest.raises(CardNotSupported):
        model([
            lesson(1, lesson_number=None, lesson_label="кл.час", subject="Классный час"),
            lesson(2, lesson_number=None, lesson_label="кл.час", subject="Классный час"),
        ])


def test_missing_room_has_no_empty_badge_or_invented_label(drawing):
    texts, boxes = drawing
    render_card(model([lesson(2, room=None)]))
    assert not any(fill == "#26454B" for _, fill in boxes)
    assert any(item["value"] == "Иванов И. И." for item in texts)
    assert not any("ауд" in item["value"] or item["value"] == "None" for item in texts)


@pytest.mark.parametrize("room,teacher", [
    ("24", "Иванов И. И."),
    ("ауд. 24а", "Константинопольская Александра Александровна"),
    (None, "Константинопольская Александра Александровна"),
])
def test_complete_teacher_has_a_measured_gap_before_time(drawing, room, teacher):
    texts, boxes = drawing
    card = model([lesson(2, teacher=teacher, room=room)])
    render_card(card)
    teacher_text, = [item for item in texts if item["value"] == teacher]
    time_text, = [item for item in texts if item["value"] == "10:40–12:10"
                  and item["y"] == teacher_text["y"]]
    assert teacher_text["right"] <= time_text["left"] - 20
    assert teacher_text["y"] == time_text["y"]
    assert time_text["anchor"] == "rt"
    if room:
        coords, = [coords for coords, fill in boxes if fill == "#26454B"]
        assert coords[2] <= teacher_text["left"] - 16


def test_unreadable_teacher_uses_fallback_instead_of_truncation():
    card = model([lesson(2, teacher="Александровская " * 9)])
    with pytest.raises(CardNotSupported):
        render_card(card)


@pytest.mark.parametrize("number", [0, 6, 7, 8, 10, None])
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


def test_typography_measures_scaled_ink_and_rejects_overflow():
    typography = _Typography()
    sample = "Й"
    _, top, _, bottom = typography.font(30).getbbox(sample, anchor="lt")
    max_height = (bottom - top) / 2 - 1
    lines, font, size = typography.fit(sample, 100, sizes=range(30, 16, -1), max_height=max_height)
    left, top, right, bottom = font.getbbox(lines[0], anchor="lt")
    assert bottom - top <= max_height * 2
    assert size < 30
    assert right - left <= 100 * 2
    with pytest.raises(CardNotSupported):
        typography.fit(sample, 100, sizes=range(30, 29, -1), max_height=max_height)
