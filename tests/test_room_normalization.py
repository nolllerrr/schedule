from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import date

import pytest

from schedule_bot.change_policy import should_notify
from schedule_bot.domain import Lesson, LessonSegment, ParsedSchedule, ScheduleChange
from schedule_bot.matching import compare_lessons
from schedule_bot.publications import PublicationInput
from schedule_bot.repository import ScheduleRepository


DAY = date(2026, 10, 29)


def lesson(room: str | None) -> Lesson:
    return Lesson(
        DAY, "ИС-22", 1, "1 пара", 1, "09:00", "10:30",
        "МДК.01.04.", "Паталах Ю.А.", room,
    )


@pytest.mark.parametrize("room,expected", [
    ("46.0", "46"),
    ("45.00 / 46.0", "45 / 46"),
    ("43.0/42.0", "43/42"),
    (None, None),
    ("", ""),
    ("001", "001"),
    ("46.5", "46.5"),
    ("46а", "46а"),
    ("46.0а", "46.0а"),
    ("1.0.2", "1.0.2"),
    ("спортзал", "спортзал"),
])
def test_room_display_and_serialization_preserve_identifiers(room, expected):
    slot = replace(lesson(room), raw_data={"N107": room or ""})
    part = LessonSegment(slot.subject, slot.teacher, room, "1")

    assert slot.room == slot.to_dict()["room"] == expected
    assert part.room == part.to_dict()["room"] == expected
    assert slot.raw_data == {"N107": room or ""}


def test_room_format_does_not_hide_real_moves_or_other_assignment_changes():
    before = lesson("45")
    same = lesson("45.0")
    assert compare_lessons([before], [same]) == ()
    assert not should_notify(ScheduleChange("modified", before, same))

    moved = replace(same, position=2, lesson_label="2 пара", lesson_number=2)
    assert [change.kind for change in compare_lessons([before], [moved])] == ["moved"]

    for after in (
        lesson("46.0"), lesson("45.5"), lesson("45а"),
        replace(same, teacher="Мельник Н.Л."),
        replace(same, subject="Другой предмет"),
        replace(same, start_time="09:30"),
    ):
        changes = compare_lessons([before], [after])
        assert len(changes) == 1
        assert should_notify(changes[0])


def test_subgroup_room_format_keeps_assignments_and_teacher_projection():
    before = lesson("45").with_segments((
        LessonSegment("Математика", "Преподаватель А", "45", "1"),
        LessonSegment("Физика", "Преподаватель Б", "46", "2"),
    ))
    after = before.with_segments(tuple(
        replace(part, room=part.room + ".0") for part in reversed(before.segments)
    ))

    assert compare_lessons([before], [after]) == ()
    assert not should_notify(ScheduleChange("modified", before, after))
    assert after.for_teacher("Преподаватель А").room == "45"


@pytest.mark.parametrize("publications", [False, True])
@pytest.mark.parametrize("segmented", [False, True])
@pytest.mark.parametrize("stored_room,incoming_room", [("45.0", "45"), ("45", "45.0")])
def test_reimport_of_stored_room_format_does_not_enqueue_notifications(
    tmp_path, publications, segmented, stored_room, incoming_room,
):
    repository = ScheduleRepository(tmp_path / "schedule.db")
    original = lesson("45")
    if segmented:
        original = original.with_segments((
            LessonSegment(original.subject, original.teacher, "45", "1"),
        ))

    def import_slot(slot, digest):
        parsed = ParsedSchedule(
            "29.10.2026.xlsx", DAY, DAY, (DAY,), (slot,),
            date_groups={DAY: ("ИС-22",)},
        )
        if publications:
            return repository.reconcile_publications([
                PublicationInput("https://source.test/schedule.xlsx", digest, parsed),
            ])
        return repository.import_schedule(parsed, sha256=digest)

    import_slot(original, "before")
    # Simulate rows written by the previous version, bypassing construction.
    with closing(sqlite3.connect(repository.database_path)) as connection, connection:
        parts = [dict(part.to_dict(), room=stored_room) for part in original.segments]
        connection.execute(
            "UPDATE lessons SET room=?, segments_json=?",
            (stored_room, json.dumps(parts)),
        )
    assert repository.lessons_for(role="student", target="ИС-22", lesson_date=DAY)[0].room == "45"
    repository.save_profile(1, role="student", target="ИС-22")
    repository.save_profile(2, role="teacher", target=original.teacher)
    repository.save_chat_profile(
        -100123, chat_type="supergroup", chat_title="ИС-22",
        target="ИС-22", configured_by=1,
    )

    incoming = replace(original, room=incoming_room)
    if segmented:
        incoming = incoming.with_segments(tuple(
            replace(part, room=incoming_room) for part in original.segments
        ))
    result = import_slot(incoming, "after")

    assert not result.skipped
    assert result.changes == ()
    assert repository.notification_outbox_counts() == {}
    assert repository.lessons_for(role="student", target="ИС-22", lesson_date=DAY)[0].room == "45"
