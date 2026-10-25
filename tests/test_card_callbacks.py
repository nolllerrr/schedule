from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import SendPhoto
from aiogram.types import BufferedInputFile

from schedule_bot.bot import app, cards, groups
from schedule_bot.bot.menus import callback_data
from schedule_bot.card_renderer import CardNotSupported
from schedule_bot.card_service import CardBusy, CardService, CardTimeout
from schedule_bot.domain import Lesson, LessonSegment, ParsedSchedule
from schedule_bot.repository import ScheduleRepository


DAY = date(2026, 10, 26)
OWNER = 123
CHAT = -100123


@pytest.fixture
def repository(tmp_path: Path) -> ScheduleRepository:
    repo = ScheduleRepository(tmp_path / "schedule.db")
    lessons = tuple(
        Lesson(
            lesson_date=DAY + timedelta(days=offset), group_name=group,
            position=1, lesson_label="2 пара", lesson_number=2,
            start_time="10:40", end_time="12:10", subject=subject,
        )
        for offset in (0, 1)
        for group, subject in (("ИС-22", "Математика"), ("ПД-12", "Право"))
    )
    repo.import_schedule(
        ParsedSchedule("26.-27.10.2026.xlsx", DAY, DAY + timedelta(days=1),
                       (DAY, DAY + timedelta(days=1)), lessons),
        sha256="cards-fixture",
    )
    repo.save_profile(OWNER, role="student", target="ИС-22")
    repo.save_chat_profile(
        CHAT, chat_type="supergroup", chat_title="Учебный чат",
        target="ПД-12", configured_by=OWNER,
    )
    return repo


@pytest.fixture
def service(monkeypatch):
    service = SimpleNamespace(get_card=AsyncMock(return_value=b"synthetic-png"))
    monkeypatch.setattr(cards, "get_card_service", lambda: service)
    return service


def callback(prefix: str, action: str, *, owner: int = OWNER, user: int = OWNER):
    return SimpleNamespace(
        data=callback_data(prefix, owner, action),
        from_user=SimpleNamespace(id=user),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=OWNER if prefix == "pnav" else CHAT,
                                 type="private" if prefix == "pnav" else "supergroup"),
            answer=AsyncMock(), answer_photo=AsyncMock(), edit_text=AsyncMock(),
        ),
        answer=AsyncMock(),
    )


async def invoke(value, repository):
    settings = SimpleNamespace(timezone="Europe/Moscow")
    analytics = SimpleNamespace(track=AsyncMock())
    if value.data.startswith("pnav:"):
        await app.private_menu_action(value, repository, settings, analytics)
    else:
        await groups.group_menu_action(value, None, repository, settings, analytics)


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix,target,subject", [
    ("pnav", "ИС-22", "Математика"), ("gnav", "ПД-12", "Право"),
])
async def test_card_uses_stored_profile_and_sends_a_new_photo(
    repository, service, prefix, target, subject,
):
    value = callback(prefix, f"card_{DAY}")
    await invoke(value, repository)

    requested = service.get_card.call_args.kwargs
    assert requested["role"] == "student"
    assert requested["target"] == target
    assert requested["lesson_date"] == DAY
    assert [item.subject for item in requested["lessons"]] == [subject]
    photo = value.message.answer_photo.call_args
    assert isinstance(photo.args[0], BufferedInputFile)
    assert photo.args[0].data == b"synthetic-png"
    assert "Расписание ·" not in (photo.kwargs["caption"] or "")
    assert photo.kwargs["parse_mode"] is None
    assert len((photo.kwargs["caption"] or "").encode("utf-16-le")) // 2 <= 1024
    value.message.edit_text.assert_not_called()
    value.answer.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["pnav", "gnav"])
async def test_other_menu_owner_cannot_request_card(repository, service, prefix):
    value = callback(prefix, f"card_{DAY}", user=456)
    await invoke(value, repository)
    service.get_card.assert_not_called()
    value.message.answer_photo.assert_not_called()
    assert value.answer.call_args.kwargs["show_alert"]


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["pnav", "gnav"])
@pytest.mark.parametrize("action", [
    "card_2026-02-30", "card_2026-10-26:ПД-12", "card_2026-10-26_teacher",
    "card_20261026", "card_2026-W44-1",
])
async def test_card_rejects_forged_or_invalid_date(repository, service, prefix, action):
    value = callback(prefix, action)
    await invoke(value, repository)
    service.get_card.assert_not_called()
    value.message.answer_photo.assert_not_called()
    assert value.answer.call_args.kwargs["show_alert"]


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["pnav", "gnav"])
async def test_unpublished_date_has_no_empty_card(repository, service, prefix):
    value = callback(prefix, "card_2026-11-01")
    await invoke(value, repository)
    service.get_card.assert_not_called()
    value.message.answer_photo.assert_not_called()
    notice = value.message.answer.call_args.args[0]
    assert "не опубликовано" in notice
    assert "выходной" not in notice.casefold()


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["pnav", "gnav"])
@pytest.mark.parametrize("action,day,back", [
    ("today", DAY, "root"),
    ("tomorrow", DAY + timedelta(days=1), "root"),
    (f"date_{DAY}", DAY, "root"),
    (f"weekdate_{DAY}", DAY, "week"),
])
async def test_day_results_keep_navigation_and_offer_correct_card(
    repository, prefix, action, day, back, monkeypatch,
):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 26, 9, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(app, "datetime", Clock)
    monkeypatch.setattr(groups, "_today", lambda settings: DAY)
    value = callback(prefix, action)
    await invoke(value, repository)
    markup = value.message.edit_text.call_args.kwargs["reply_markup"]
    buttons = [button for row in markup.inline_keyboard for button in row]
    assert any(button.text == "🖼 Карточка" and
               button.callback_data == f"{prefix}:{OWNER}:card_{day}"
               for button in buttons)
    assert any(button.callback_data == f"{prefix}:{OWNER}:{back}" for button in buttons)
    assert all(len(button.callback_data.encode("utf-8")) <= 64 for button in buttons)
    if prefix == "gnav":
        assert any(button.callback_data == f"{prefix}:{OWNER}:settings" for button in buttons)


@pytest.mark.asyncio
async def test_teacher_has_no_card_button_and_cannot_forge_one(repository, service):
    repository.save_profile(OWNER, role="teacher", target="Иванов И.И.")
    result = callback("pnav", f"date_{DAY}")
    await invoke(result, repository)
    markup = result.message.edit_text.call_args.kwargs["reply_markup"]
    assert not any("card_" in button.callback_data
                   for row in markup.inline_keyboard for button in row)
    forged = callback("pnav", f"card_{DAY}")
    await invoke(forged, repository)
    assert "учебных групп" in forged.answer.call_args.args[0]
    service.get_card.assert_not_called()


@pytest.mark.asyncio
async def test_freshness_is_recomputed_for_each_caption(repository, service, monkeypatch):
    monkeypatch.setattr(repository, "schedule_freshness", lambda: {
        "last_accepted_at": "2026-01-01T08:00:00+00:00",
    })
    stale = callback("pnav", f"card_{DAY}")
    await invoke(stale, repository)
    assert "может быть устаревшим" in stale.message.answer_photo.call_args.kwargs["caption"]
    monkeypatch.setattr(repository, "schedule_freshness", lambda: {
        "last_accepted_at": datetime.now(timezone.utc).isoformat(),
    })
    fresh = callback("pnav", f"card_{DAY}")
    await invoke(fresh, repository)
    assert fresh.message.answer_photo.call_args.kwargs["caption"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("error,phrase", [
    (CardNotSupported("private-secret"), "без потери сведений"),
    (CardBusy(), "другие карточки"),
    (CardTimeout(), "слишком много времени"),
    (RuntimeError("private-secret"), "временно недоступна"),
])
async def test_renderer_failures_preserve_text_menu(repository, service, error, phrase, caplog):
    service.get_card.side_effect = error
    value = callback("pnav", f"card_{DAY}")
    await invoke(value, repository)
    assert phrase in value.message.answer.call_args.args[0]
    assert "private-secret" not in caplog.text
    value.message.edit_text.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["subgroups", "class-hour-subgroup", "overflow"])
async def test_real_renderer_fallback_keeps_complete_assignment_text(
    tmp_path, monkeypatch, kind,
):
    if kind == "subgroups":
        parts = (
            LessonSegment("Английский язык", "Иванова А. А.", "201", "1"),
            LessonSegment("Немецкий язык", "Петров Б. Б.", "305", "2"),
        )
        lessons = (Lesson(DAY, "ИС-22", 2, "2 пара", 2, "10:40", "12:10",
                          "Иностранный язык", segments=parts),)
        expected = ("Английский язык", "Иванова А. А.", "201", "Подгруппа 1",
                    "Немецкий язык", "Петров Б. Б.", "305", "Подгруппа 2")
    elif kind == "class-hour-subgroup":
        parts = (
            LessonSegment("Классный час", "Соколова А. В.", "104", "1"),
            LessonSegment("Классный час", "Иванова А. А.", "201", "2"),
        )
        lessons = (Lesson(DAY, "ИС-22", 1, "кл.час", None, "08:00", "08:30",
                          "Классный час", segments=parts),)
        expected = ("Классный час", "Соколова А. В.", "104", "08:00–08:30", "Подгруппа 1",
                    "Иванова А. А.", "201", "Подгруппа 2")
    else:
        subject = "Разработка " * 25
        lessons = (Lesson(DAY, "ИС-22", 2, "2 пара", 2, "10:40", "12:10",
                          subject, "Орлов Д. С.", "312"),)
        expected = (subject.strip(), "Орлов Д. С.", "312", "10:40–12:10")
    repository = ScheduleRepository(tmp_path / "fallback.db")
    repository.import_schedule(
        ParsedSchedule("26.10.2026.xlsx", DAY, DAY, (DAY,), lessons),
        sha256=f"fallback-{kind}",
    )
    repository.save_profile(OWNER, role="student", target="ИС-22")
    renderer = CardService()
    monkeypatch.setattr(cards, "get_card_service", lambda: renderer)
    try:
        value = callback("pnav", f"date_{DAY}")
        await invoke(value, repository)
        original_text = value.message.edit_text.call_args.args[0]
        for field in expected:
            assert field in original_text
        value.message.edit_text.reset_mock()
        value.data = callback_data("pnav", OWNER, f"card_{DAY}")
        await invoke(value, repository)
        assert "без потери сведений" in value.message.answer.call_args.args[0]
        value.message.answer_photo.assert_not_called()
        value.message.edit_text.assert_not_called()
    finally:
        await renderer.aclose()


@pytest.mark.asyncio
async def test_class_hour_day_sends_real_png_and_keeps_text_menu(tmp_path, monkeypatch):
    lessons = (
        Lesson(DAY, "ИС-22", 2, "2 пара", 2, "10:30", "11:40", "Математика"),
        Lesson(DAY, "ИС-22", 3, "кл.час", None, "11:50", "12:20",
               "Классный час", "Соколова А. В.", "104"),
        Lesson(DAY, "ИС-22", 4, "3 пара", 3, "13:00", "14:00", "Базы данных"),
    )
    repository = ScheduleRepository(tmp_path / "class-hour.db")
    repository.import_schedule(
        ParsedSchedule("26.10.2026.xlsx", DAY, DAY, (DAY,), lessons), sha256="class-hour",
    )
    repository.save_profile(OWNER, role="student", target="ИС-22")
    renderer = CardService()
    monkeypatch.setattr(cards, "get_card_service", lambda: renderer)
    try:
        value = callback("pnav", f"date_{DAY}")
        await invoke(value, repository)
        assert "Классный час" in value.message.edit_text.call_args.args[0]
        value.message.edit_text.reset_mock()
        value.data = callback_data("pnav", OWNER, f"card_{DAY}")
        await invoke(value, repository)
        photo = value.message.answer_photo.call_args.args[0]
        with Image.open(BytesIO(photo.data)) as image:
            assert image.size == (1080, 1796)
        value.message.answer.assert_not_called()
        value.message.edit_text.assert_not_called()
    finally:
        await renderer.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("exception", [TelegramBadRequest, TelegramNetworkError])
async def test_telegram_photo_failure_is_explained_without_losing_menu(
    repository, service, exception, caplog,
):
    value = callback("gnav", f"card_{DAY}")
    value.message.answer_photo.side_effect = exception(
        method=SendPhoto(chat_id=CHAT, photo="image"), message="private-secret",
    )
    await invoke(value, repository)
    assert "Не удалось отправить" in value.message.answer.call_args.args[0]
    assert "private-secret" not in caplog.text
    value.message.edit_text.assert_not_called()


@pytest.mark.asyncio
async def test_failed_explanation_does_not_raise(repository, service):
    value = callback("pnav", "card_2026-11-01")
    value.message.answer.side_effect = TelegramNetworkError(
        method=SendPhoto(chat_id=OWNER, photo="image"), message="offline",
    )
    await invoke(value, repository)


@pytest.mark.asyncio
async def test_group_card_uses_current_chat_group_after_settings_change(repository, service):
    # A previously displayed button has only a date, never a writable group.
    value = callback("gnav", f"card_{DAY}")
    repository.save_chat_profile(
        CHAT, chat_type="supergroup", chat_title="Учебный чат",
        target="ИС-22", configured_by=OWNER,
    )
    await invoke(value, repository)
    assert service.get_card.call_args.kwargs["target"] == "ИС-22"


@pytest.mark.asyncio
async def test_direct_result_keeps_text_reply_keyboard_and_adds_card_control(repository):
    message = SimpleNamespace(from_user=SimpleNamespace(id=OWNER), answer=AsyncMock())
    settings = SimpleNamespace(timezone="Europe/Moscow")
    await app._send_schedule(
        message, repository, settings, SimpleNamespace(track=AsyncMock()),
        selected_date=DAY, scope="date",
    )
    assert message.answer.await_count == 2
    text_call, control_call = message.answer.call_args_list
    assert "Математика" in text_call.args[0]
    assert "—10:40–12:10—" in text_call.args[0]
    assert text_call.kwargs["reply_markup"].keyboard
    assert control_call.kwargs["reply_markup"].inline_keyboard[0][0].callback_data == (
        f"pnav:{OWNER}:card_{DAY}"
    )


@pytest.mark.asyncio
async def test_legacy_date_result_also_offers_owner_bound_card(repository):
    value = callback("pnav", f"date_{DAY}")
    value.data = f"date:{DAY}"
    await app.selected_date(
        value, repository, SimpleNamespace(timezone="Europe/Moscow"),
        SimpleNamespace(track=AsyncMock()),
    )
    assert value.message.answer.await_count == 2
    assert value.message.answer.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data == (
        f"pnav:{OWNER}:card_{DAY}"
    )


@pytest.mark.asyncio
async def test_missing_profile_cannot_use_an_old_card_button(repository, service):
    value = callback("pnav", f"card_{DAY}", owner=456, user=456)
    await invoke(value, repository)
    assert "/start" in value.answer.call_args.args[0]
    service.get_card.assert_not_called()


@pytest.mark.asyncio
async def test_foreign_chat_cannot_use_card_button_without_its_profile(repository, service):
    value = callback("gnav", f"card_{DAY}")
    value.message.chat.id = -100999
    await invoke(value, repository)
    assert "не настроен" in value.answer.call_args.args[0]
    service.get_card.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["day", "week"])
async def test_optional_card_control_failure_keeps_text_and_analytics(
    repository, scope, monkeypatch, caplog,
):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 26, 9, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(app, "datetime", Clock)
    failure = TelegramNetworkError(
        method=SendPhoto(chat_id=OWNER, photo="image"), message="private-secret",
    )
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=OWNER),
        answer=AsyncMock(side_effect=[SimpleNamespace(), failure]),
    )
    analytics = SimpleNamespace(track=AsyncMock())
    settings = SimpleNamespace(timezone="Europe/Moscow")
    if scope == "day":
        await app._send_schedule(
            message, repository, settings, analytics, selected_date=DAY, scope="date",
        )
    else:
        await app.week(message, repository, settings, analytics)
    text_call = message.answer.call_args_list[0]
    assert "Математика" in text_call.args[0]
    assert text_call.kwargs["reply_markup"].keyboard
    analytics.track.assert_awaited_once()
    assert "private-secret" not in caplog.text
