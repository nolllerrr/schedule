from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher, F, Router
from aiogram.methods import GetMe, SendMessage, SendPhoto
from aiogram.types import Message, Update, User

from schedule_bot.bot import app, cards, report_admin, reports
from schedule_bot.domain import Lesson, ParsedSchedule
from schedule_bot.repository import ScheduleRepository
from schedule_bot.reports.models import ReportError
from schedule_bot.reports.service import ReportService
from test_reports import DAY, NOW, context

SETTINGS = SimpleNamespace(admin_telegram_ids=(900, 901), timezone="Europe/Moscow")


@pytest.fixture
def repo(tmp_path):
    repository = ScheduleRepository(tmp_path / "reports.db")
    repository.initialize()
    return repository


@pytest.fixture
def interface(repo, monkeypatch):
    monkeypatch.setattr("schedule_bot.reports.storage.timestamp", lambda now=None: now or NOW)
    monkeypatch.setattr(cards, "get_card_service", lambda: SimpleNamespace(get_card=AsyncMock(return_value=b"png")))
    async def send(bot, method, **kwargs):
        if isinstance(method, GetMe):
            return User(id=123456, is_bot=True, first_name="Test", username="reports_test_bot")
        if isinstance(method, (SendMessage, SendPhoto)):
            return Message.model_validate({"message_id": 77, "date": NOW,
                                           "chat": {"id": method.chat_id, "type": "private" if method.chat_id > 0 else "group"}})
        return True
    session = AsyncMock(side_effect=send)
    bot = Bot("123456:synthetic-test-token", session=session)
    dispatcher = Dispatcher()
    dispatcher.message.outer_middleware(reports.ReportNavigationMiddleware())
    for source in (reports.router, report_admin.router, app.router):
        cloned = Router()
        cloned.message.filter(F.chat.type == "private")
        cloned.callback_query.filter(F.message.chat.type == "private")
        cloned.message.handlers.extend(source.message.handlers)
        cloned.callback_query.handlers.extend(source.callback_query.handlers)
        dispatcher.include_router(cloned)

    async def feed(*, user=100, text=None, callback=None, message_id=77, photo=False):
        sender = {"id": user, "is_bot": False, "first_name": "Test"}
        message = {"message_id": message_id, "date": NOW, "chat": {"id": user, "type": "private"}, "from": sender}
        if text is not None:
            message["text"] = text
        if photo:
            message["photo"] = [{"file_id": "synthetic", "file_unique_id": "synthetic", "width": 10, "height": 10}]
        event = {"message": message} if callback is None else {"callback_query": {
            "id": "callback", "from": sender, "chat_instance": "test", "data": callback, "message": message,
        }}
        return await dispatcher.feed_update(bot, Update.model_validate({"update_id": 1, **event}), repository=repo,
                                            settings=SETTINGS, analytics=SimpleNamespace(track=AsyncMock()))
    return SimpleNamespace(feed=feed, bot=bot, session=session, dispatcher=dispatcher)


def last_message(interface):
    return [call.args[1] for call in interface.session.call_args_list if isinstance(call.args[1], SendMessage)][-1]


@pytest.mark.asyncio
async def test_full_persistent_dialog_through_dispatcher(repo, interface):
    token = context(repo)
    await interface.feed(callback="rpc:" + token)
    assert "В каком занятии" in last_message(interface).text
    draft = repo.report_active_draft(100, now=NOW)
    await interface.feed(callback=f'rpd:{draft["id"]}:0:lesson:l0s0')
    await interface.feed(callback=f'rpd:{draft["id"]}:1:category:room')
    # Draft is recovered by SQLite filters, without a matching in-memory FSM.
    await interface.feed(text="<b>Аудитория</b> должна быть 47")
    confirmation = last_message(interface)
    assert confirmation.parse_mode is None and "<b>Аудитория</b>" in confirmation.text
    send = confirmation.reply_markup.inline_keyboard[0][0].callback_data
    await interface.feed(callback=send)
    assert "#1 зарегистрировано" in last_message(interface).text
    await interface.feed(callback=send)
    assert "#1 зарегистрировано" in last_message(interface).text
    assert len(repo.report_pending(now=NOW)) == 2


@pytest.mark.asyncio
async def test_group_link_preempts_start_without_profile(repo, interface):
    token = context(repo, source="group")
    for user in (100, 101):
        await interface.feed(user=user, text="/start report_" + token)
        assert "В каком занятии" in last_message(interface).text
        assert repo.get_profile(user) is None
        assert repo.report_active_draft(user, now=NOW)
    draft = repo.report_active_draft(100, now=NOW)
    await interface.feed(text="/start report_" + token)
    assert repo.report_active_draft(100, now=NOW)["id"] == draft["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/start", "/menu"])
async def test_main_commands_cancel_draft_and_teacher_fsm(repo, interface, text):
    repo.report_open_draft(context(repo), 100, now=NOW)
    state = interface.dispatcher.fsm.get_context(bot=interface.bot, chat_id=100, user_id=100)
    await state.set_state(app.ProfileSetup.waiting_teacher_query)
    await interface.feed(text=text)
    assert repo.report_active_draft(100, now=NOW) is None
    assert await state.get_state() is None
    assert "Преподаватель не найден" not in last_message(interface).text


@pytest.mark.asyncio
async def test_private_forgery_and_source_message_binding(repo, interface):
    token = context(repo)
    repo.report_bind_context(token, 100, 77)
    await interface.feed(user=101, callback="rpc:" + token)
    assert "недоступна" in last_message(interface).text
    await interface.feed(callback="rpc:" + token, message_id=78)
    assert "недоступна" in last_message(interface).text
    assert repo.report_active_draft(100, now=NOW) is None


@pytest.mark.asyncio
async def test_attachments_commands_and_stale_callbacks(repo, interface):
    draft = repo.report_open_draft(context(repo), 100, now=NOW)
    await interface.feed(callback=f'rpd:{draft["id"]}:0:lesson:day')
    await interface.feed(callback=f'rpd:{draft["id"]}:1:category:room')
    await interface.feed(photo=True)
    assert "Вложения" in last_message(interface).text
    await interface.feed(text="/unrecognized")
    assert repo.report_active_draft(100, now=NOW)["stage"] == "description"
    await interface.feed(callback=f'rpd:{draft["id"]}:0:lesson:day')
    assert "устарела" in last_message(interface).text
    await interface.feed(callback=f'rpd:{draft["id"]}:2:category:injected')
    assert repo.report_active_draft(100, now=NOW)["stage"] == "description"


@pytest.mark.asyncio
async def test_buttons_snapshots_and_format_switch(repo, interface):
    lesson = Lesson(DAY, "ИС-22", 1, "1 пара", 1, "09:00", "10:30", "Предмет", "Учитель", "45")
    repo.import_schedule(ParsedSchedule("old.xlsx", DAY, DAY, (DAY,), (lesson,)), sha256="old")
    repo.save_profile(100, role="student", target="ИС-22")
    incoming = Message.model_validate({"message_id": 1, "date": NOW, "chat": {"id": 100, "type": "private"}}).as_(interface.bot)
    await cards.send_current_schedule_view(incoming, repo, SETTINGS, owner_id=100, target="ИС-22", lesson_date=DAY, lessons=[lesson])
    photo = interface.session.call_args.args[1]
    assert isinstance(photo, SendPhoto)
    assert [row[0].text for row in photo.reply_markup.inline_keyboard] == ["Текст", "⚠️ Сообщить об ошибке", "Меню расписания"]
    old_token = photo.reply_markup.inline_keyboard[1][0].callback_data[4:]
    repo.import_schedule(ParsedSchedule("new.xlsx", DAY, DAY, (DAY,), (replace(lesson, room="47"),)), sha256="new")
    repo.save_profile(100, role="student", target="OTHER")
    old = repo.report_open_draft(old_token, 100, now=NOW)
    assert '"room":"45"' in old["snapshot_json"] and old["target"] == "ИС-22"
    repo.save_profile(100, role="student", target="ИС-22")
    callback = SimpleNamespace(data="pnav:100:view_text_2026-10-29", from_user=SimpleNamespace(id=100),
                               message=incoming, answer=AsyncMock())
    await cards.switch_schedule_view(callback, repo, SETTINGS, owner_id=100, role="student", target="ИС-22", action="view_text_2026-10-29")
    text = last_message(interface)
    assert text.reply_markup.inline_keyboard[0][0].text == "Карточка"
    new_token = text.reply_markup.inline_keyboard[1][0].callback_data[4:]
    assert '"room":"47"' in repo.report_open_draft(new_token, 100, now=NOW)["snapshot_json"]
    for markup in (photo.reply_markup, text.reply_markup):
        assert all(len(button.callback_data.encode()) <= 64 for row in markup.inline_keyboard for button in row)


@pytest.mark.asyncio
async def test_group_schedule_button_is_url_and_sharable(repo, interface):
    lesson = Lesson(DAY, "ИС-22", 1, "1 пара", 1, None, None, "Предмет")
    incoming = Message.model_validate({"message_id": 1, "date": NOW, "chat": {"id": -100, "type": "group"}}).as_(interface.bot)
    await cards.send_current_schedule_view(incoming, repo, SETTINGS, owner_id=55, target="ИС-22", lesson_date=DAY, lessons=[lesson], prefix="gnav")
    photo = interface.session.call_args.args[1]
    button = photo.reply_markup.inline_keyboard[1][0]
    assert button.callback_data is None and button.url.startswith("https://t.me/reports_test_bot?start=report_")
    payload = button.url.split("?start=")[1]
    assert len(payload) <= 64 and "ИС-22" not in payload
    for user in (100, 101):
        await interface.feed(user=user, text="/start " + payload)
        assert repo.report_active_draft(user, now=NOW)["source_chat_id"] == -100


@pytest.mark.asyncio
async def test_report_failure_does_not_prevent_schedule(repo, interface, monkeypatch):
    monkeypatch.setattr(repo, "report_create_context", lambda **kwargs: (_ for _ in ()).throw(OSError("private token")))
    incoming = Message.model_validate({"message_id": 1, "date": NOW, "chat": {"id": 100, "type": "private"}}).as_(interface.bot)
    await cards.send_current_schedule_view(incoming, repo, SETTINGS, owner_id=100, target="ИС-22", lesson_date=DAY, lessons=[])
    assert len(interface.session.call_args.args[1].reply_markup.inline_keyboard) == 2


@pytest.mark.asyncio
async def test_feature_disabled_at_actions_not_only_buttons(repo):
    token = context(repo)
    with pytest.raises(ReportError):
        await ReportService(repo, ()).open(token, 100, now=NOW)


@pytest.mark.asyncio
async def test_admin_routes_and_confirmation(repo, interface):
    from test_reports import ready
    draft = ready(repo)
    report = repo.report_submit(draft["id"], 100, draft["revision"], (900,), now=NOW)
    await interface.feed(user=100, callback=f'rpa:{report["id"]}:reply')
    assert "только администратору" in last_message(interface).text
    await interface.feed(user=900, callback=f'rpa:{report["id"]}:reply')
    await interface.feed(user=900, text="Проверил исходный файл")
    confirm = last_message(interface).reply_markup.inline_keyboard[0][0].callback_data
    await interface.feed(user=900, callback=confirm)
    with repo._connect() as connection:
        assert connection.execute("SELECT destination_id FROM report_outbox WHERE kind='reply'").fetchone()[0] == 100


@pytest.mark.asyncio
async def test_notification_report_keeps_outbox_snapshot_after_import(repo, interface, monkeypatch):
    import json
    from datetime import datetime, timedelta, timezone
    from schedule_bot.notifier import ScheduleNotifier
    monkeypatch.setattr("schedule_bot.notifier.schedule_photo", AsyncMock(return_value=None))
    lesson = Lesson(DAY, "ИС-22", 1, "1 пара", 1, None, None, "Предмет", "Учитель", "45")
    repo.save_profile(100, role="student", target="ИС-22")
    first = repo.import_schedule(ParsedSchedule("old.xlsx", DAY, DAY, (DAY,), (lesson,)), sha256="old")
    repo.import_schedule(ParsedSchedule("new.xlsx", DAY, DAY, (DAY,), (replace(lesson, room="47"),)), sha256="new")
    clock = lambda: datetime.now(timezone.utc) + timedelta(minutes=1)
    worker = ScheduleNotifier(interface.bot, repo, (900,), broadcast_interval=0, private_interval=0, clock=clock)
    assert first.import_id
    assert await worker.deliver_pending() >= 1
    sent = next(call.args[1] for call in interface.session.call_args_list
                if isinstance(call.args[1], SendMessage) and call.args[1].reply_markup is not None)
    control = sent.reply_markup.inline_keyboard[1][0]
    draft = repo.report_open_draft(control.callback_data[4:], 100, now=NOW)
    assert json.loads(draft["snapshot_json"])["lessons"][0]["room"] == "45"


@pytest.mark.asyncio
async def test_long_assignment_list_has_persistent_pages(repo, interface):
    lessons = [Lesson(DAY, "ИС-22", index, f"{index} пара", index, None, None, "Предмет") for index in range(30)]
    token = context(repo, lessons=lessons)
    await interface.feed(callback="rpc:" + token)
    draft = repo.report_active_draft(100, now=NOW)
    assert len(last_message(interface).reply_markup.inline_keyboard) <= 16
    await interface.feed(callback=f'rpd:{draft["id"]}:0:page:1')
    assert repo.report_active_draft(100, now=NOW)["lesson_page"] == 1
    assert "13 пара" in last_message(interface).reply_markup.inline_keyboard[1][0].text
    await interface.feed(callback=f'rpd:{draft["id"]}:1:lesson:l13s0')
    assert repo.report_active_draft(100, now=NOW)["selected_lesson_key"] == "l13s0"
