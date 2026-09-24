from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from schedule_bot.change_policy import should_notify, subject_changed
from schedule_bot.domain import Lesson, ParsedSchedule, ScheduleChange
from schedule_bot.notifier import ScheduleNotifier
from schedule_bot.pinning import sync_chat_schedule_pin
from schedule_bot.presentation import format_changes
from schedule_bot.repository import ScheduleRepository


LESSON_DATE = date(2026, 9, 25)
ORIGINAL_SUBJECT = "МДК.01.01. / Разр.прогр.мод."
CLARIFIED_SUBJECT = "МДК.01.01. КП / Разр.прогр.мод."


def _lesson(*, subject: str = ORIGINAL_SUBJECT, room: str = "41") -> Lesson:
    return Lesson(
        lesson_date=LESSON_DATE,
        group_name="ИС-22",
        position=2,
        lesson_label="2 пара",
        lesson_number=2,
        start_time="10:50",
        end_time="12:20",
        subject=subject,
        teacher="Иванова И.И.",
        room=room,
    )


def _parsed(lesson: Lesson) -> ParsedSchedule:
    return ParsedSchedule(
        source_filename="25.09.2026.xlsx",
        start_date=LESSON_DATE,
        end_date=LESSON_DATE,
        dates=(LESSON_DATE,),
        lessons=(lesson,),
    )


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.edited: list[tuple[int, int, str]] = []
        self.pinned: list[tuple[int, int]] = []

    async def send_message(self, chat_id: int, text: str) -> SimpleNamespace:
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=100 + len(self.sent))

    async def edit_message_text(
        self, text: str, *, chat_id: int, message_id: int
    ) -> None:
        self.edited.append((chat_id, message_id, text))

    async def pin_chat_message(
        self, *, chat_id: int, message_id: int, disable_notification: bool
    ) -> None:
        self.pinned.append((chat_id, message_id))


def test_course_project_label_is_not_a_subject_change() -> None:
    assert not subject_changed(ORIGINAL_SUBJECT, CLARIFIED_SUBJECT)
    assert not should_notify(
        ScheduleChange(
            "modified",
            _lesson(),
            _lesson(subject=CLARIFIED_SUBJECT),
        )
    )


@pytest.mark.parametrize(
    "changed_subject",
    [
        "МДК.01.02. / Разр.прогр.мод.",
        "МДК.01.01. / Тестирование программ",
        "Математика КП",
    ],
)
def test_real_subject_change_still_notifies(changed_subject: str) -> None:
    assert subject_changed(ORIGINAL_SUBJECT, changed_subject)
    assert should_notify(
        ScheduleChange("modified", _lesson(), _lesson(subject=changed_subject))
    )


def test_room_change_still_notifies_without_cosmetic_subject_line() -> None:
    change = ScheduleChange(
        "modified",
        _lesson(),
        _lesson(subject=CLARIFIED_SUBJECT, room="42"),
    )

    assert should_notify(change)
    text = format_changes((change,))
    assert "• Аудитория: 41 → 42" in text
    assert "• Предмет:" not in text


@pytest.mark.asyncio
async def test_clarification_updates_database_and_pin_without_chat_message(
    tmp_path: Path,
) -> None:
    repository = ScheduleRepository(tmp_path / "schedule.db")
    repository.import_schedule(_parsed(_lesson()), sha256="before")
    repository.save_chat_profile(
        -100123,
        chat_type="supergroup",
        chat_title="ИС-22",
        target="ИС-22",
        configured_by=1,
        pin_enabled=True,
    )
    bot = FakeBot()
    now = datetime(2026, 9, 25, 9, 0)

    assert await sync_chat_schedule_pin(  # type: ignore[arg-type]
        bot, repository, chat_id=-100123, target="ИС-22", now=now
    )
    first_hash = repository.get_chat_profile(-100123)["last_pinned_schedule_hash"]

    result = repository.import_schedule(
        _parsed(replace(_lesson(), subject=CLARIFIED_SUBJECT)),
        sha256="after",
    )
    assert len(result.changes) == 1
    await ScheduleNotifier(bot, repository, ()).notify_import(result)  # type: ignore[arg-type]
    assert repository.notification_outbox_counts() == {}
    assert len(bot.sent) == 1

    assert await sync_chat_schedule_pin(  # type: ignore[arg-type]
        bot, repository, chat_id=-100123, target="ИС-22", now=now
    )
    assert len(bot.sent) == 1
    assert len(bot.edited) == 1
    assert CLARIFIED_SUBJECT in bot.edited[0][2]
    assert bot.pinned == [(-100123, 101)]
    profile = repository.get_chat_profile(-100123)
    assert profile["last_pinned_message_id"] == 101
    assert profile["last_pinned_schedule_hash"] != first_hash
    assert repository.lessons_for(
        role="student", target="ИС-22", lesson_date=LESSON_DATE
    )[0].subject == CLARIFIED_SUBJECT
