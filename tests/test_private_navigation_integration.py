import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from schedule_bot.bot import app, cards
from schedule_bot.card_renderer import CardNotSupported
from schedule_bot.domain import Lesson, ParsedSchedule
from schedule_bot.repository import ScheduleRepository

DAY = date(2026, 10, 26)
OWNER = 123
SETTINGS = SimpleNamespace(timezone="Europe/Moscow")


@pytest.fixture
def navigation(tmp_path, monkeypatch):
    path = tmp_path / "navigation.db"
    repository = ScheduleRepository(path)
    dates = tuple(DAY + timedelta(days=offset) for offset in range(9))
    lessons = (
        Lesson(dates[0], "ИС-22", 1, "1 пара", 1, "09:00", "10:10", "Математика", "Иванов И.И.", "24"),
        Lesson(dates[1], "ПД-12", 1, "1 пара", 1, "09:00", "10:10", "Чужое право", "Петров П.П.", "31"),
        Lesson(dates[3], "ИС-22", 1, "1 пара", 1, "09:00", "10:10", "Физика", "Иванов И.И.", "25"),
        Lesson(dates[3], "ПД-12", 2, "2 пара", 2, "10:30", "11:40", "Общая физика", "Иванов И.И.", "30"),
        Lesson(dates[7], "ИС-22", 1, "1 пара", 1, "09:00", "10:10", "Вне недели", "Иванов И.И.", "26"),
    )
    repository.import_schedule(ParsedSchedule("schedule.xlsx", dates[0], dates[-1], dates, lessons), sha256="navigation")
    repository.save_profile(OWNER, role="student", target="ИС-22")

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 26, 6, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(app, "datetime", Clock)
    renderer = SimpleNamespace(get_card=AsyncMock(side_effect=CardNotSupported()))
    monkeypatch.setattr(cards, "get_card_service", lambda: renderer)
    return repository, path


def database_snapshot(path):
    with closing(sqlite3.connect(path)) as connection:
        return tuple(connection.iterdump())


def message():
    return SimpleNamespace(
        from_user=SimpleNamespace(id=OWNER), answer=AsyncMock(), answer_photo=AsyncMock(),
        edit_text=AsyncMock(), delete=AsyncMock(),
    )


def callback(data, user=OWNER):
    return SimpleNamespace(data=data, from_user=SimpleNamespace(id=user), message=message(), answer=AsyncMock())


def buttons(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


@pytest.mark.asyncio
@pytest.mark.parametrize("action,expected_date,expected_subject", [
    ("today", DAY, "Математика"),
    ("tomorrow", DAY + timedelta(days=1), None),
    ("date_2026-10-29", DAY + timedelta(days=3), "Физика"),
    ("weekdate_2026-10-29", DAY + timedelta(days=3), "Физика"),
    ("date_2026-11-20", date(2026, 11, 20), None),
])
async def test_private_menu_day_selection_is_scoped_and_read_only(navigation, action, expected_date, expected_subject):
    repository, path = navigation
    before = database_snapshot(path)
    value = callback(f"pnav:{OWNER}:{action}")
    analytics = SimpleNamespace(track=AsyncMock())
    await app.private_menu_action(value, repository, SETTINGS, analytics)
    result = value.message.answer.call_args
    assert f"{expected_date.day} {'октября' if expected_date.month == 10 else 'ноября'}" in result.args[0]
    assert "ИС-22" in result.args[0] and "Чужое право" not in result.args[0]
    if expected_subject:
        assert expected_subject in result.args[0]
    else:
        assert "занятий нет или расписание ещё не опубликовано" in result.args[0]
    assert f"pnav:{OWNER}:view_card_{expected_date}" in buttons(result.kwargs["reply_markup"])
    value.message.delete.assert_awaited_once()
    assert database_snapshot(path) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("handler,expected", [(app.today, "Математика"), (app.tomorrow, "занятий нет")])
async def test_day_buttons_return_correct_day_without_changing_database(navigation, handler, expected):
    repository, path = navigation
    before = database_snapshot(path)
    value = message()
    await handler(value, repository, SETTINGS, SimpleNamespace(track=AsyncMock()))
    result = value.answer.call_args.args[0]
    assert expected in result and "Чужое право" not in result
    assert ("26 октября" if handler is app.today else "27 октября") in result
    assert database_snapshot(path) == before


@pytest.mark.asyncio
async def test_next_study_day_skips_days_without_own_lessons(navigation):
    repository, path = navigation
    before = database_snapshot(path)
    value = message()
    analytics = SimpleNamespace(track=AsyncMock())
    await app.next_study_day(value, repository, SETTINGS, analytics)
    text = value.answer.call_args.args[0]
    assert "29 октября" in text and "Физика" in text
    assert "Чужое право" not in text and "Математика" not in text
    assert analytics.track.call_args.kwargs["properties"]["scope"] == "next"
    assert database_snapshot(path) == before


@pytest.mark.asyncio
async def test_teacher_navigation_uses_teacher_profile_and_skips_other_teachers(navigation):
    repository, path = navigation
    repository.save_profile(OWNER, role="teacher", target="Петров П.П.")
    before = database_snapshot(path)
    analytics = SimpleNamespace(track=AsyncMock())
    value = message()
    await app.next_study_day(value, repository, SETTINGS, analytics)
    text = value.answer.call_args.args[0]
    assert "27 октября" in text and "Чужое право" in text
    assert "Математика" not in text and "Физика" not in text
    weekly = callback(f"pnav:{OWNER}:week")
    await app.private_menu_action(weekly, repository, SETTINGS, analytics)
    assert buttons(weekly.message.edit_text.call_args.kwargs["reply_markup"]) == [
        f"pnav:{OWNER}:weekdate_2026-10-27", f"pnav:{OWNER}:root",
    ]
    assert database_snapshot(path) == before


@pytest.mark.asyncio
async def test_teacher_selected_day_shows_all_their_groups_only(navigation):
    repository, path = navigation
    repository.save_profile(OWNER, role="teacher", target="Иванов И.И.")
    before = database_snapshot(path)
    value = callback(f"pnav:{OWNER}:date_2026-10-29")
    await app.private_menu_action(value, repository, SETTINGS, SimpleNamespace(track=AsyncMock()))
    text = value.message.answer.call_args.args[0]
    assert "29 октября" in text and "ИС-22" in text and "ПД-12" in text
    assert "Общая физика" in text and "Физика" in text
    assert "Чужое право" not in text and "Петров П.П." not in text
    assert database_snapshot(path) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("menu", [False, True])
async def test_week_shows_only_own_days_within_seven_day_window(navigation, menu):
    repository, path = navigation
    before = database_snapshot(path)
    value = callback(f"pnav:{OWNER}:week") if menu else message()
    if menu:
        await app.private_menu_action(value, repository, SETTINGS, SimpleNamespace(track=AsyncMock()))
        result = value.message.edit_text.call_args
    else:
        await app.week(value, repository, SETTINGS, SimpleNamespace(track=AsyncMock()))
        result = value.answer.call_args
    assert "выберите день" in result.args[0].lower()
    assert buttons(result.kwargs["reply_markup"]) == [
        f"pnav:{OWNER}:weekdate_2026-10-26", f"pnav:{OWNER}:weekdate_2026-10-29", f"pnav:{OWNER}:root",
    ]
    assert database_snapshot(path) == before


@pytest.mark.asyncio
async def test_date_selector_and_selection_use_published_dates(navigation):
    repository, path = navigation
    before = database_snapshot(path)
    menu = callback(f"pnav:{OWNER}:dates")
    await app.private_menu_action(menu, repository, SETTINGS, SimpleNamespace(track=AsyncMock()))
    markup = menu.message.edit_text.call_args.kwargs["reply_markup"]
    assert f"pnav:{OWNER}:date_2026-10-29" in buttons(markup)
    selected = callback(f"pnav:{OWNER}:date_2026-10-29")
    await app.private_menu_action(selected, repository, SETTINGS, SimpleNamespace(track=AsyncMock()))
    assert "Физика" in selected.message.answer.call_args.args[0]
    assert database_snapshot(path) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("data,user", [
    ("pnav:broken:today", OWNER), ("pnav:123:date_2026-02-30", OWNER),
    ("pnav:123:dates_page_-1", OWNER), ("pnav:123:unknown", OWNER),
    ("pnav:123:today", 456),
])
async def test_expired_malformed_and_foreign_menu_do_not_disclose_or_mutate(navigation, data, user):
    repository, path = navigation
    before = database_snapshot(path)
    value = callback(data, user)
    analytics = SimpleNamespace(track=AsyncMock())
    await app.private_menu_action(value, repository, SETTINGS, analytics)
    assert value.answer.call_args.kwargs["show_alert"] is True
    value.message.answer.assert_not_called()
    value.message.answer_photo.assert_not_called()
    value.message.edit_text.assert_not_called()
    value.message.delete.assert_not_called()
    analytics.track.assert_not_called()
    assert database_snapshot(path) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["date:2026-02-30", "date:not-a-date"])
async def test_invalid_legacy_date_does_not_send_or_modify_data(navigation, data):
    repository, path = navigation
    before = database_snapshot(path)
    value = callback(data)
    analytics = SimpleNamespace(track=AsyncMock())
    await app.selected_date(value, repository, SETTINGS, analytics)
    assert "Некорректная дата" in value.answer.call_args.args[0]
    assert value.answer.call_args.kwargs["show_alert"] is True
    value.message.answer.assert_not_called()
    value.message.answer_photo.assert_not_called()
    value.message.delete.assert_not_called()
    analytics.track.assert_not_called()
    assert database_snapshot(path) == before


@pytest.mark.asyncio
async def test_no_published_schedule_leaves_navigation_and_database_unchanged(tmp_path, monkeypatch):
    path = tmp_path / "empty.db"
    repository = ScheduleRepository(path)
    repository.save_profile(OWNER, role="student", target="ИС-22")

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 26, 6, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(app, "datetime", Clock)
    before = database_snapshot(path)
    analytics = SimpleNamespace(track=AsyncMock())
    for handler, text in ((app.next_study_day, "пока не опубликован"), (app.week, "занятий нет")):
        value = message()
        await handler(value, repository, SETTINGS, analytics)
        assert text in value.answer.call_args.args[0]
    value = message()
    await app.choose_date(value, repository, SETTINGS)
    assert "нет расписания" in value.answer.call_args.args[0]
    menu = callback(f"pnav:{OWNER}:dates")
    await app.private_menu_action(menu, repository, SETTINGS, analytics)
    assert "нет расписания" in menu.answer.call_args.args[0]
    assert menu.answer.call_args.kwargs["show_alert"] is True
    assert database_snapshot(path) == before
