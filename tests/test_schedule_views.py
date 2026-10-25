from datetime import date, datetime, timezone
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import EditMessageText, SendRichMessage
from aiogram.types import Message
from PIL import Image
import pytest

from schedule_bot.bot import app, cards
from schedule_bot.bot.menus import callback_data, schedule_view_keyboard
from schedule_bot.card_renderer import CardNotSupported
from schedule_bot.card_service import CardBusy, CardService, CardTimeout
from schedule_bot.domain import Lesson, ParsedSchedule
from schedule_bot.repository import ScheduleRepository


DAY = date(2026, 10, 26)
OWNER = 123


@pytest.fixture
def repository(tmp_path, monkeypatch):
    repo = ScheduleRepository(tmp_path / "views.db")
    lessons = (
        Lesson(DAY, "ИС-22", 2, "2 пара", 2, "10:30", "11:40", "Математика", "Иванов И.И.", "24"),
        Lesson(DAY, "ПД-12", 2, "2 пара", 2, "10:30", "11:40", "Право", "Петров П.П.", "31"),
    )
    repo.import_schedule(ParsedSchedule("26.10.2026.xlsx", DAY, DAY, (DAY,), lessons), sha256="views")
    repo.save_profile(OWNER, role="student", target="ИС-22")

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 26, 6, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(app, "datetime", Clock)
    return repo


@pytest.fixture
def service(monkeypatch):
    service = SimpleNamespace(get_card=AsyncMock(return_value=b"synthetic-png"))
    monkeypatch.setattr(cards, "get_card_service", lambda: service)
    return service


def callback(action, *, owner=OWNER, user=OWNER):
    return SimpleNamespace(
        data=callback_data("pnav", owner, action),
        from_user=SimpleNamespace(id=user),
        message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock(), answer_photo=AsyncMock()),
        answer=AsyncMock(),
    )


async def invoke(value, repository):
    await app.private_menu_action(
        value, repository, SimpleNamespace(timezone="Europe/Moscow"),
        SimpleNamespace(track=AsyncMock()),
    )


@pytest.mark.parametrize("as_card,label,action", [(True, "Текст", "text"), (False, "Карточка", "card")])
def test_view_keyboard_has_one_owner_bound_button(as_card, label, action):
    markup = schedule_view_keyboard(9223372036854775807, DAY, as_card=as_card)
    assert len(markup.inline_keyboard) == 1
    button, = markup.inline_keyboard[0]
    assert button.text == label
    assert button.callback_data == f"pnav:9223372036854775807:view_{action}_{DAY}"
    assert len(button.callback_data.encode("utf-8")) <= 64


@pytest.mark.asyncio
async def test_round_trip_edits_the_same_message_using_real_aiogram_requests(repository, monkeypatch):
    session = AsyncMock(return_value=True)
    bot = Bot("123456:synthetic-test-token", session=session)
    renderer = CardService()
    monkeypatch.setattr(cards, "get_card_service", lambda: renderer)

    def message(message_id):
        return Message.model_validate({
            "message_id": message_id, "date": datetime.now(timezone.utc),
            "chat": {"id": OWNER, "type": "private"},
            "from": {"id": OWNER, "is_bot": False, "first_name": "Тест"},
            "text": "Щас",
        }).as_(bot)

    try:
        await app.show_current_schedule(
            message(10), repository, SimpleNamespace(timezone="Europe/Moscow"),
            SimpleNamespace(track=AsyncMock()),
        )
        initial = session.call_args.args[1]
        assert isinstance(initial, SendRichMessage)
        assert session.await_count == 1
        assert initial.reply_markup.inline_keyboard[0][0].text == "Текст"
        png = initial.rich_message.blocks[0].photo.media.data
        with Image.open(BytesIO(png)) as image:
            assert image.size == (1080, 1620)
        for action in (f"view_text_{DAY}", f"view_card_{DAY}"):
            value = callback(action)
            value.message = message(900)
            await invoke(value, repository)
        initial, text, card = [call.args[1] for call in session.call_args_list]
        assert isinstance(text, EditMessageText) and isinstance(card, EditMessageText)
        assert text.message_id == card.message_id == 900
        assert text.chat_id == card.chat_id == OWNER
        assert "Математика" in text.rich_message.html
        assert text.rich_message.blocks is None and text.rich_message.media is None
        assert text.reply_markup.inline_keyboard[0][0].text == "Карточка"
        assert card.reply_markup.inline_keyboard[0][0].text == "Текст"
        assert card.rich_message.blocks[0].photo.media.data is png
    finally:
        await renderer.aclose()
        await bot.session.close()


@pytest.mark.asyncio
async def test_text_toggle_reads_updated_profile_and_keeps_callback_date(repository, service, monkeypatch):
    repository.save_profile(OWNER, role="student", target="ПД-12")
    monkeypatch.setattr(app, "datetime", datetime)  # Callback date must not depend on the current clock.
    value = callback(f"view_text_{DAY}")
    await invoke(value, repository)
    edit = value.message.edit_text.call_args
    assert "ПД-12" in edit.kwargs["rich_message"].html
    assert "Право" in edit.kwargs["rich_message"].html
    assert "Математика" not in edit.kwargs["rich_message"].html
    assert edit.kwargs["reply_markup"].inline_keyboard[0][0].callback_data == f"pnav:{OWNER}:view_card_{DAY}"
    service.get_card.assert_not_called()
    value.message.answer.assert_not_called()
    value.message.answer_photo.assert_not_called()


@pytest.mark.asyncio
async def test_card_toggle_uses_current_data_and_recomputes_freshness(repository, service, monkeypatch):
    repository.save_profile(OWNER, role="student", target="ПД-12")
    monkeypatch.setattr(repository, "schedule_freshness", lambda: {"last_accepted_at": "2026-01-01T08:00:00+00:00"})
    value = callback(f"view_card_{DAY}")
    await invoke(value, repository)
    assert service.get_card.call_args.kwargs["target"] == "ПД-12"
    assert service.get_card.call_args.kwargs["lessons"][0].subject == "Право"
    content = value.message.edit_text.call_args.kwargs["rich_message"]
    assert "может быть устаревшим" in content.blocks[0].caption.text
    monkeypatch.setattr(repository, "schedule_freshness", lambda: {"last_accepted_at": datetime.now(timezone.utc).isoformat()})
    await invoke(value, repository)
    content = value.message.edit_text.call_args.kwargs["rich_message"]
    assert content.blocks[0].caption.text == "Расписание · 26.10.2026"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["view_text_2026-02-30", "view_card_20261026", "view_card_2026-10-26_teacher", "view_raw_2026-10-26"])
async def test_invalid_view_actions_do_not_edit_or_render(repository, service, action):
    value = callback(action)
    await invoke(value, repository)
    value.message.edit_text.assert_not_called()
    service.get_card.assert_not_called()
    assert value.answer.call_args.kwargs["show_alert"]


@pytest.mark.asyncio
@pytest.mark.parametrize("access", ["other-owner", "missing-profile", "teacher"])
async def test_view_toggle_keeps_existing_authorization(repository, service, access):
    value = callback(f"view_card_{DAY}")
    if access == "other-owner":
        value.from_user.id = 456
    elif access == "missing-profile":
        value = callback(f"view_card_{DAY}", owner=456, user=456)
    else:
        repository.save_profile(OWNER, role="teacher", target="Иванов И.И.")
    await invoke(value, repository)
    value.message.edit_text.assert_not_called()
    service.get_card.assert_not_called()
    assert value.answer.call_args.kwargs["show_alert"]


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [CardNotSupported(), CardBusy(), CardTimeout(), RuntimeError("private-secret")])
async def test_unavailable_card_keeps_complete_text_in_same_message(repository, service, error, caplog):
    service.get_card.side_effect = error
    value = callback(f"view_card_{DAY}")
    await invoke(value, repository)
    edit = value.message.edit_text.call_args.kwargs
    assert "Математика" in edit["rich_message"].html
    assert "Иванов И.И." in edit["rich_message"].html
    assert "24" in edit["rich_message"].html
    assert "10:30–11:40" in edit["rich_message"].html
    assert edit["reply_markup"].inline_keyboard[0][0].text == "Карточка"
    value.message.answer.assert_not_called()
    value.message.answer_photo.assert_not_called()
    assert "private-secret" not in caplog.text


@pytest.mark.asyncio
async def test_unknown_day_switches_to_empty_text_without_inventing_day_off(repository, service):
    value = callback("view_card_2026-11-01")
    await invoke(value, repository)
    html = value.message.edit_text.call_args.kwargs["rich_message"].html
    assert "не опубликовано" in html and "выходной" not in html.casefold()
    service.get_card.assert_not_called()


@pytest.mark.asyncio
async def test_long_text_and_html_fields_are_preserved_in_one_edit(repository, service):
    subject = "<script>" + "Очень длинный предмет " * 250 + "КОНЕЦ"
    lessons = (
        Lesson(DAY, "ИС-22", 2, "2 пара", 2, "10:30", "11:40", subject),
        *repository.lessons_for(role="student", target="ПД-12", lesson_date=DAY),
    )
    repository.import_schedule(ParsedSchedule("26.10.2026.xlsx", DAY, DAY, (DAY,), lessons), sha256="long-view")
    value = callback(f"view_text_{DAY}")
    await invoke(value, repository)
    html = value.message.edit_text.call_args.kwargs["rich_message"].html
    assert len(html) > 4096
    assert "&lt;script&gt;" in html and "<script>" not in html
    assert "КОНЕЦ" in html
    value.message.edit_text.assert_awaited_once()
    value.message.answer.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("error,alert", [
    (TelegramBadRequest(method=EditMessageText(text="test"), message="message is not modified"), False),
    (TelegramNetworkError(method=EditMessageText(text="test"), message="private-secret"), True),
])
async def test_edit_errors_preserve_existing_message_and_do_not_send_replacement(repository, service, error, alert, caplog):
    value = callback(f"view_text_{DAY}")
    value.message.edit_text.side_effect = error
    await invoke(value, repository)
    value.message.answer.assert_not_called()
    value.message.answer_photo.assert_not_called()
    assert value.answer.call_args.kwargs["show_alert"] is alert
    assert "private-secret" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["render", "delivery"])
async def test_current_card_failure_delivers_text_with_reverse_button(repository, service, failure):
    message = SimpleNamespace(from_user=SimpleNamespace(id=OWNER), answer=AsyncMock(), answer_rich=AsyncMock())
    if failure == "render":
        service.get_card.side_effect = CardNotSupported()
    else:
        message.answer_rich.side_effect = TelegramNetworkError(
            method=SendRichMessage(chat_id=OWNER, rich_message={"html": "test"}), message="network",
        )
    await app.show_current_schedule(
        message, repository, SimpleNamespace(timezone="Europe/Moscow"), SimpleNamespace(track=AsyncMock()),
    )
    if failure == "render":
        message.answer.assert_not_called()
        content = message.answer_rich.call_args
        assert "Математика" in content.args[0].html
        assert content.args[0].blocks is None
    else:
        message.answer.assert_awaited_once()
        content = message.answer.call_args
        assert "Математика" in content.args[0]
    button = content.kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == "Карточка" and button.callback_data == f"pnav:{OWNER}:view_card_{DAY}"
