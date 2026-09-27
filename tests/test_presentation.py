from datetime import date

import pytest

from schedule_bot.domain import Lesson, ScheduleChange
from schedule_bot.presentation import (
    format_changes,
    format_date,
    format_schedule,
    format_week,
)


LESSON_DATE = date(2026, 9, 24)


def lesson(
    *,
    position: int = 2,
    number: int | None = 2,
    label: str = "2 пара",
    subject: str = "Разработка программных модулей",
    start: str | None = "10:50",
    end: str | None = "12:20",
    teacher: str | None = "Мельник Н.Л.",
    room: str | None = "45/46",
    group: str = "ИС-22",
    subgroup: str | None = None,
) -> Lesson:
    return Lesson(
        lesson_date=LESSON_DATE,
        group_name=group,
        position=position,
        lesson_label=label,
        lesson_number=number,
        start_time=start,
        end_time=end,
        subject=subject,
        teacher=teacher,
        room=room,
        subgroup=subgroup,
    )


def test_format_date_uses_russian_month_and_weekday() -> None:
    assert format_date(LESSON_DATE) == "24 сентября, четверг"


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (1, "1 пара"),
        (2, "2 пары"),
        (5, "5 пар"),
        (11, "11 пар"),
        (21, "21 пара"),
        (22, "22 пары"),
    ],
)
def test_student_lesson_count_has_correct_plural(count: int, expected: str) -> None:
    lessons = [
        lesson(
            position=index,
            number=None,
            label=f"{index} пара",
            start=None,
            end=None,
        )
        for index in range(1, count + 1)
    ]

    assert f"<b>{expected}</b>" in format_schedule(
        lessons, "ИС-22", LESSON_DATE, "student"
    )


@pytest.mark.parametrize(
    ("count", "expected"),
    [(1, "1 занятие"), (2, "2 занятия"), (5, "5 занятий"), (21, "21 занятие")],
)
def test_teacher_lesson_count_has_correct_plural(count: int, expected: str) -> None:
    lessons = [lesson(position=index) for index in range(1, count + 1)]

    assert f"<b>{expected}</b>" in format_schedule(
        lessons, "Мельник Н.Л.", LESSON_DATE, "teacher"
    )


def test_schedule_emphasizes_subject_and_keeps_metadata_compact() -> None:
    lessons = [
        lesson(),
        lesson(
            position=3,
            number=3,
            label="3 пара",
            subject="Машинное обучение",
            start="13:00",
            end="14:30",
            teacher="Бабуха О.О.",
            room="41/42",
        ),
    ]

    result = format_schedule(lessons, "ИС-22", LESSON_DATE, "student")

    assert result.startswith("📚 <b>Группа ИС-22</b>\n24 сентября, четверг")
    assert "<b>2 пары</b> • 10:50–14:30" in result
    assert (
        "2️⃣ <b>45/46</b> ауд. | <b>Разработка программных модулей</b>"
        "\n—10:50–12:20—\nМельник Н.Л."
    ) in result
    assert "3️⃣ <b>41/42</b> ауд. | <b>Машинное обучение</b>" in result


def test_teacher_schedule_shows_group_in_secondary_line() -> None:
    result = format_schedule([lesson()], "Мельник Н.Л.", LESSON_DATE, "teacher")

    assert "📚 <b>Преподаватель Мельник Н.Л.</b>" in result
    assert "<b>1 занятие</b>" in result
    assert "2️⃣ <b>45/46</b> ауд. | <b>Разработка программных модулей</b>" in result
    assert "—10:50–12:20—\nгруппа ИС-22" in result
    assert "Мельник Н.Л." not in result.split("\n", 3)[-1]


def test_schedule_escapes_every_excel_and_target_value() -> None:
    unsafe = lesson(
        label="<2 пара>",
        number=None,
        subject="Python <async> & SQL",
        teacher='Иванова <А.А.> & "Ко"',
        room="<45&46>",
        group="ИС<22>",
        subgroup="<1>",
    )

    result = format_schedule([unsafe], "ИС<22> & test", LESSON_DATE, "student")

    assert "ИС&lt;22&gt; &amp; test" in result
    assert (
        "&lt;2 пара&gt; <b>&lt;45&amp;46&gt;</b> ауд. | "
        "<b>Python &lt;async&gt; &amp; SQL</b>"
    ) in result
    assert "<b>&lt;45&amp;46&gt;</b> ауд." in result
    assert "Иванова &lt;А.А.&gt; &amp; &quot;Ко&quot;" in result
    assert "подгр. &lt;1&gt;" in result
    assert "Python <async>" not in result


def test_empty_schedule_is_clear() -> None:
    result = format_schedule([], "ИС-22", LESSON_DATE, "student")

    assert "Группа ИС-22" in result
    assert "24 сентября, четверг" in result
    assert "На этот день занятий нет или расписание ещё не опубликовано." in result


@pytest.mark.parametrize("weekly", [False, True])
@pytest.mark.parametrize(
    ("label", "subject"),
    [("кл.час", "Классный час"), (" КЛ. ЧАС ", "Обсуждение"),
     ("", "Классный\u00a0час")],
)
def test_class_hour_is_visible_but_not_counted_as_pair(weekly, label, subject):
    lessons = [
        lesson(position=n, number=n, label=f"{n} пара", start="09:00", end="15:10")
        for n in range(1, 5)
    ]
    lessons.append(lesson(position=3, number=None, label=label, subject=subject,
                          start="11:50", end="12:20", room="42"))
    result = (
        format_week({LESSON_DATE: lessons}, "ИС-22", "student")
        if weekly else format_schedule(lessons, "ИС-22", LESSON_DATE, "student")
    )
    assert "<b>4 пары</b> • 09:00–15:10" in result
    assert "<b>5 пар</b>" not in result
    assert f"<b>{subject}</b>" in result
    assert "—11:50–12:20—" in result


def test_day_with_only_class_hour_is_not_empty():
    result = format_schedule(
        [lesson(number=None, label="кл.час", subject="Классный час")],
        "ИС-22", LESSON_DATE, "student",
    )
    assert "<b>1 классный час</b>" in result
    assert "занятий нет" not in result


def test_teacher_class_hour_remains_an_activity():
    result = format_schedule(
        [lesson(), lesson(number=None, label="кл.час", subject="Классный час")],
        "Мельник Н.Л.", LESSON_DATE, "teacher",
    )
    assert "<b>2 занятия</b>" in result


def test_week_formats_days_and_an_empty_day() -> None:
    next_day = date(2026, 9, 25)
    result = format_week(
        {LESSON_DATE: [lesson()], next_day: []}, "ИС-22", "student"
    )

    assert result.startswith("📅 <b>Расписание на неделю</b>\n<b>Группа ИС-22</b>")
    assert "<b>24 сентября, четверг</b>" in result
    assert "<b>1 пара</b> • 10:50–12:20" in result
    assert "<b>25 сентября, пятница</b>\nЗанятий нет." in result


def test_empty_week_is_clear_and_escapes_target() -> None:
    result = format_week({}, "ИС<&>", "student")

    assert "<b>Группа ИС&lt;&amp;&gt;</b>" in result
    assert "Расписание на эту неделю ещё не опубликовано." in result


def test_changes_show_concrete_before_and_after_values() -> None:
    before = lesson(
        subject="Алгебра <база>",
        start="10:50",
        end="12:20",
        teacher="Иванова & Ко",
        room="41",
    )
    after = lesson(
        subject="Геометрия > практика",
        start="13:00",
        end="14:30",
        teacher="Петров П.П.",
        room="42",
    )

    result = format_changes([ScheduleChange("modified", before, after)])

    assert "✏️ <b>Изменено: 2 пара · ИС-22</b>" in result
    assert "• Предмет: Алгебра &lt;база&gt; → Геометрия &gt; практика" in result
    assert "• Время: 10:50–12:20 → 13:00–14:30" in result
    assert "• Преподаватель: Иванова &amp; Ко → Петров П.П." in result
    assert "• Аудитория: 41 → 42" in result
    assert "Алгебра <база>" not in result


def test_changes_describe_added_and_removed_lessons() -> None:
    added = lesson(subject="Новый предмет", teacher=None, room=None)
    removed = lesson(
        position=3,
        number=3,
        label="3 пара",
        subject="Старый предмет",
        start="13:00",
        end="14:30",
    )

    result = format_changes(
        [
            ScheduleChange("added", None, added),
            ScheduleChange("removed", removed, None),
        ]
    )

    assert "➕ <b>Добавлено: 2 пара · ИС-22</b>" in result
    assert "<b>Новый предмет</b>" in result
    assert "➖ <b>Удалено: 3 пара · ИС-22</b>" in result
    assert "<b>Старый предмет</b>" in result


def test_empty_changes_are_clear() -> None:
    assert format_changes([]) == "🔄 <b>Изменений в расписании нет</b>"
