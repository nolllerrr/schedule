from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import DeleteMessage, SendMessage, SendPhoto
from aiogram.types import Message

from schedule_bot.bot import cards, groups
from schedule_bot.card_renderer import CardNotSupported
from schedule_bot.domain import Lesson, ParsedSchedule
from schedule_bot.repository import ScheduleRepository


DAY = date(2026, 10, 26)
CHAT = -100123
OWNER = 123
SETTINGS = SimpleNamespace(timezone="Europe/Moscow")


@pytest.fixture
def repository(tmp_path):
    repo = ScheduleRepository(tmp_path / "group-views.db")
    lessons = tuple(
        Lesson(day, target, 2, "2 пара", 2, "10:30", "11:40", subject)
        for day in (DAY, DAY + timedelta(days=1))
        for target, subject in (("ИС-22", "Математика"), ("ПД-12", "Право"))
    )
    repo.import_schedule(
        ParsedSchedule("26-27.10.2026.xlsx", DAY, DAY + timedelta(days=1),
                       (DAY, DAY + timedelta(days=1)), lessons), sha256="group-views",
    )
    repo.save_profile(OWNER, role="student", target="ИС-22")
    repo.save_chat_profile(CHAT, chat_type="supergroup", chat_title="Учебный чат",
                           target="ПД-12", configured_by=OWNER)
    return repo


@pytest.fixture
def service(monkeypatch):
    renderer = SimpleNamespace(get_card=AsyncMock(return_value=b"synthetic-png"))
    monkeypatch.setattr(cards, "get_card_service", lambda: renderer)
    monkeypatch.setattr(groups, "_now", lambda settings: datetime(2026, 10, 26, 9, tzinfo=ZoneInfo(settings.timezone)))
    return renderer


def message():
    return SimpleNamespace(
        chat=SimpleNamespace(id=CHAT, type="supergroup"), from_user=SimpleNamespace(id=OWNER),
        answer=AsyncMock(), answer_photo=AsyncMock(), delete=AsyncMock(), edit_text=AsyncMock(),
    )


def callback(action):
    return SimpleNamespace(data=f"gnav:{OWNER}:{action}", from_user=SimpleNamespace(id=OWNER),
                           message=message(), answer=AsyncMock())


async def invoke(value, repository):
    await groups.group_menu_action(value, None, repository, SETTINGS, SimpleNamespace(track=AsyncMock()))


@pytest.mark.asyncio
@pytest.mark.parametrize("now,expected", [
    (datetime(2026, 10, 25, 12), DAY),
    (datetime(2026, 10, 26, 9), DAY),
    (datetime(2026, 10, 26, 11, 39), DAY),
    (datetime(2026, 10, 26, 11, 40), DAY + timedelta(days=1)),
])
async def test_menu_opens_current_card_for_chat_group(repository, service, monkeypatch, now, expected):
    monkeypatch.setattr(groups, "_now", lambda settings: now.replace(tzinfo=ZoneInfo(settings.timezone)))
    incoming = message()
    await groups.open_group_menu(incoming, repository, SETTINGS)
    requested = service.get_card.call_args.kwargs
    assert requested["target"] == "ПД-12"
    assert requested["lesson_date"] == expected
    assert requested["lessons"][0].subject == "Право"
    incoming.answer.assert_not_called()
    incoming.delete.assert_not_called()
    sent = incoming.answer_photo.call_args
    assert sent.args[0].data == b"synthetic-png"
    buttons = [button for row in sent.kwargs["reply_markup"].inline_keyboard for button in row]
    assert [button.callback_data for button in buttons] == [
        f"gnav:{OWNER}:view_text_{expected}", f"gnav:{OWNER}:root",
    ]


@pytest.mark.asyncio
async def test_menu_round_trip_and_navigation_use_real_group_requests(repository, service):
    session = AsyncMock(return_value=True)
    bot = Bot("123456:synthetic-test-token", session=session)

    def real_message(message_id, *, photo=False):
        data = {
            "message_id": message_id, "date": datetime(2026, 10, 26, 9),
            "chat": {"id": CHAT, "type": "supergroup", "title": "Учебный чат"},
            "from": {"id": OWNER, "is_bot": False, "first_name": "Тест"},
        }
        if photo:
            data["photo"] = [{"file_id": "test", "file_unique_id": "test", "width": 1080, "height": 1444}]
        else:
            data["text"] = "/menu"
        return Message.model_validate(data).as_(bot)

    try:
        await groups.open_group_menu(real_message(10), repository, SETTINGS)
        for action, message_id, photo in ((f"view_text_{DAY}", 900, True),
                                          (f"view_card_{DAY}", 901, False), ("root", 902, True)):
            value = callback(action)
            value.message = real_message(message_id, photo=photo)
            await invoke(value, repository)
        requests = [call.args[1] for call in session.call_args_list]
        assert [type(request) for request in requests] == [
            SendPhoto, SendMessage, DeleteMessage, SendPhoto, DeleteMessage, SendMessage, DeleteMessage,
        ]
        assert all(request.chat_id == CHAT for request in requests)
        assert [request.message_id for request in requests if isinstance(request, DeleteMessage)] == [900, 901, 902]
        assert "Право" in requests[1].text and "Математика" not in requests[1].text
        assert requests[1].reply_markup.inline_keyboard[0][0].callback_data == f"gnav:{OWNER}:view_card_{DAY}"
        assert requests[3].reply_markup.inline_keyboard[0][0].callback_data == f"gnav:{OWNER}:view_text_{DAY}"
        menu_buttons = [button.callback_data for row in requests[5].reply_markup.inline_keyboard for button in row]
        assert f"gnav:{OWNER}:settings" in menu_buttons and f"gnav:{OWNER}:today" in menu_buttons
    finally:
        await bot.session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["render", "delivery"])
async def test_menu_card_failure_sends_text_with_group_buttons(repository, service, failure):
    incoming = message()
    if failure == "render":
        service.get_card.side_effect = CardNotSupported()
    else:
        incoming.answer_photo.side_effect = TelegramNetworkError(
            method=SendPhoto(chat_id=CHAT, photo="test"), message="network",
        )
    await groups.open_group_menu(incoming, repository, SETTINGS)
    sent = incoming.answer.call_args
    assert "Право" in sent.args[0]
    assert sent.kwargs["reply_markup"].inline_keyboard[0][0].callback_data == f"gnav:{OWNER}:view_card_{DAY}"


@pytest.mark.asyncio
async def test_menu_without_upcoming_schedule_keeps_date_and_settings_navigation(repository, service, monkeypatch):
    monkeypatch.setattr(groups, "_now", lambda settings: datetime(2026, 10, 28, 9, tzinfo=ZoneInfo(settings.timezone)))
    incoming = message()
    await groups.open_group_menu(incoming, repository, SETTINGS)
    service.get_card.assert_not_called()
    assert "пока не опубликовано" in incoming.answer.call_args.args[0]
    assert incoming.answer.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data == f"gnav:{OWNER}:today"


@pytest.mark.asyncio
@pytest.mark.parametrize("access", ["other-owner", "missing-chat", "inactive-chat"])
@pytest.mark.parametrize("view", ["text", "card"])
async def test_group_view_rejects_unauthorized_or_unconfigured_chat(repository, service, access, view):
    value = callback(f"view_{view}_{DAY}")
    if access == "other-owner":
        value.from_user.id = 456
    elif access == "missing-chat":
        value.message.chat.id = -100999
    else:
        repository.deactivate_chat(CHAT)
    await invoke(value, repository)
    service.get_card.assert_not_called()
    value.message.answer.assert_not_called()
    value.message.answer_photo.assert_not_called()
    value.message.delete.assert_not_called()
    assert value.answer.call_args.kwargs["show_alert"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("access", ["missing-chat", "inactive-chat"])
async def test_menu_requires_active_chat_setup(repository, service, access):
    incoming = message()
    if access == "missing-chat":
        incoming.chat.id = -100999
    else:
        repository.deactivate_chat(CHAT)
    await groups.open_group_menu(incoming, repository, SETTINGS)
    service.get_card.assert_not_called()
    assert "/setup" in incoming.answer.call_args.args[0]


@pytest.mark.asyncio
async def test_group_view_uses_updated_chat_group(repository, service):
    repository.save_chat_profile(CHAT, chat_type="supergroup", chat_title="Учебный чат",
                                 target="ИС-22", configured_by=OWNER)
    value = callback(f"view_card_{DAY}")
    await invoke(value, repository)
    assert service.get_card.call_args.kwargs["target"] == "ИС-22"
    value.message.answer_photo.assert_awaited_once()
    value.message.delete.assert_awaited_once()
