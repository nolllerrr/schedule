from dataclasses import replace
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from schedule_bot.bot import app, cards
from schedule_bot.domain import Lesson, ParsedSchedule
from schedule_bot.repository import ScheduleRepository
from schedule_bot.schedule_selection import current_schedule


FRIDAY = date(2026, 10, 23)
SATURDAY = FRIDAY + timedelta(days=1)
MONDAY = FRIDAY + timedelta(days=3)
MOSCOW = ZoneInfo("Europe/Moscow")


def lesson(day: date, number: int, end: str) -> Lesson:
    return Lesson(
        lesson_date=day,
        group_name="ИС-22",
        position=number,
        lesson_label=f"{number} пара",
        lesson_number=number,
        start_time="09:00",
        end_time=end,
        subject=f"Предмет {number}",
        teacher="Иванов И.И." if number < 4 else "Петров П.П.",
    )


@pytest.fixture
def repository(tmp_path: Path) -> ScheduleRepository:
    repo = ScheduleRepository(tmp_path / "schedule.db")
    lessons = tuple(
        lesson(day, number, end)
        for day, ends in (
            (FRIDAY, ("10:30", "12:00", "13:30", "15:00")),
            (SATURDAY, ("10:30", "12:00", "13:30")),
            (MONDAY, ("10:30",)),
        )
        for number, end in enumerate(ends, start=1)
    )
    repo.import_schedule(
        ParsedSchedule("23.-26.10.2026.xlsx", FRIDAY, MONDAY,
                       (FRIDAY, SATURDAY, SATURDAY + timedelta(days=1), MONDAY), lessons),
        sha256="current-schedule",
    )
    return repo


@pytest.mark.parametrize("clock,expected", [
    ("2026-10-23T07:00:00", FRIDAY),
    ("2026-10-23T13:30:00", FRIDAY),
    ("2026-10-23T14:59:59", FRIDAY),
    ("2026-10-23T15:00:00", SATURDAY),
    ("2026-10-23T23:59:59", SATURDAY),
    ("2026-10-24T00:00:00", SATURDAY),
    ("2026-10-24T13:29:59", SATURDAY),
    ("2026-10-24T13:30:00", MONDAY),
    ("2026-10-25T09:00:00", MONDAY),
    ("2026-10-26T10:30:00", None),
    ("2026-10-27T09:00:00", None),
])
def test_current_day_rolls_over_at_last_lesson_end(
    repository: ScheduleRepository, clock: str, expected: date | None,
) -> None:
    selected = current_schedule(
        repository, role="student", target="ИС-22",
        now=datetime.fromisoformat(clock).replace(tzinfo=MOSCOW),
    )
    assert (selected[0] if selected else None) == expected
    if selected:
        assert all(item.lesson_date == expected for item in selected[1])


def test_teacher_uses_their_own_last_lesson(repository: ScheduleRepository) -> None:
    selected = current_schedule(
        repository, role="teacher", target="Иванов И.И.",
        now=datetime(2026, 10, 23, 13, 30, tzinfo=MOSCOW),
    )
    assert selected is not None and selected[0] == SATURDAY


@pytest.mark.parametrize("end", [None, "invalid"])
def test_unknown_end_time_keeps_today_until_midnight(
    repository: ScheduleRepository, monkeypatch: pytest.MonkeyPatch, end: str | None,
) -> None:
    original = repository.lessons_for

    def lessons_for(
        *, role: Literal["student", "teacher"], target: str, lesson_date: date,
    ) -> list[Lesson]:
        lessons = original(role=role, target=target, lesson_date=lesson_date)
        if lesson_date == FRIDAY:
            lessons[-1] = replace(lessons[-1], end_time=end)
        return lessons

    monkeypatch.setattr(repository, "lessons_for", lessons_for)
    selected = current_schedule(
        repository, role="student", target="ИС-22",
        now=datetime(2026, 10, 23, 23, 59, tzinfo=MOSCOW),
    )
    assert selected is not None and selected[0] == FRIDAY


@pytest.mark.asyncio
async def test_keyboard_button_routes_to_current_schedule() -> None:
    button = app.main_keyboard().keyboard[0][0]
    handler = next(
        handler for handler in app.router.message.handlers
        if handler.callback == app.show_current_schedule
    )
    assert handler.filters
    for text in (button.text, "Актуальное расписание"):
        assert await handler.filters[0].call(SimpleNamespace(text=text))


@pytest.mark.asyncio
@pytest.mark.parametrize("role,target", [
    ("student", "ИС-22"), ("teacher", "Иванов И.И."),
])
@pytest.mark.parametrize("utc_hour,expected_day", [(10, FRIDAY), (12, SATURDAY)])
async def test_button_uses_local_time_and_keeps_today_selection_explicit(
    repository: ScheduleRepository, monkeypatch: pytest.MonkeyPatch,
    role: Literal["student", "teacher"], target: str,
    utc_hour: int, expected_day: date,
) -> None:
    clock = datetime(2026, 10, 23, utc_hour, tzinfo=timezone.utc)

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return clock.astimezone(tz)

    monkeypatch.setattr(app, "datetime", Clock)
    repository.save_profile(123, role=role, target=target, notifications=False)
    renderer = SimpleNamespace(get_card=AsyncMock(return_value=b"synthetic-png"))
    monkeypatch.setattr(cards, "get_card_service", lambda: renderer)
    message = SimpleNamespace(from_user=SimpleNamespace(id=123), answer=AsyncMock(), answer_photo=AsyncMock())
    settings = SimpleNamespace(timezone="Europe/Moscow")
    analytics = SimpleNamespace(track=AsyncMock())

    await app.show_current_schedule(message, repository, settings, analytics)  # type: ignore[arg-type]

    if role in {"student", "teacher"}:
        message.answer.assert_not_called()
        photo_call = message.answer_photo.call_args
        assert photo_call.args[0].filename == f"schedule-{expected_day.isoformat()}.png"
        assert photo_call.kwargs["caption"] is None or "Расписание ·" not in photo_call.kwargs["caption"]
        button, = photo_call.kwargs["reply_markup"].inline_keyboard[0]
        assert button.text == "Текст"
        assert button.callback_data == f"pnav:123:view_text_{expected_day}"
        assert renderer.get_card.call_args.kwargs["lesson_date"] == expected_day
    else:
        text_call = next(call for call in message.answer.call_args_list
                         if hasattr(call.kwargs.get("reply_markup"), "keyboard"))
        assert f"{expected_day.day} октября" in text_call.args[0]
        keyboard = text_call.kwargs["reply_markup"]
        assert keyboard.keyboard[1][0].text == "Уведомления: выключены"
        message.answer_photo.assert_not_called()
    assert analytics.track.call_args.kwargs["properties"]["scope"] == "current"

    message.answer.reset_mock()
    message.answer_photo.reset_mock()
    await app.today(message, repository, settings, analytics)  # type: ignore[arg-type]
    if role in {"student", "teacher"}:
        assert message.answer_photo.call_args.args[0].filename == "schedule-2026-10-23.png"
    else:
        text_call = next(call for call in message.answer.call_args_list
                         if hasattr(call.kwargs.get("reply_markup"), "keyboard"))
        assert "23 октября" in text_call.args[0]


@pytest.mark.asyncio
async def test_button_without_profile_or_upcoming_lessons(
    repository: ScheduleRepository, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return datetime(2026, 10, 27, 9, tzinfo=MOSCOW).astimezone(tz)

    monkeypatch.setattr(app, "datetime", Clock)
    message = SimpleNamespace(from_user=SimpleNamespace(id=123), answer=AsyncMock())
    settings = SimpleNamespace(timezone="Europe/Moscow")
    analytics = SimpleNamespace(track=AsyncMock())

    await app.show_current_schedule(message, repository, settings, analytics)  # type: ignore[arg-type]
    assert "/start" in message.answer.call_args.args[0]
    analytics.track.assert_not_called()

    repository.save_profile(123, role="student", target="ИС-22")
    await app.show_current_schedule(message, repository, settings, analytics)  # type: ignore[arg-type]
    assert "пока не опубликовано" in message.answer.call_args.args[0]
    assert "не подтверждена" in message.answer.call_args.args[0]
    assert analytics.track.call_args.kwargs["properties"]["result"] == "empty"
