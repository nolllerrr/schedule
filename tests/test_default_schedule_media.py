from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageMedia
from PIL import Image

from schedule_bot import schedule_media
from schedule_bot.bot import cards, groups
from schedule_bot.card_renderer import CardNotSupported
from schedule_bot.card_service import CardService
from schedule_bot.domain import Lesson, LessonSegment, ParsedSchedule
from schedule_bot.notifier import ScheduleNotifier
from schedule_bot.pinning import sync_chat_schedule_pin
from schedule_bot.repository import ScheduleRepository


DAY = date(2026, 10, 26)
NOW = datetime(2026, 10, 26, 9, tzinfo=timezone.utc)
CHAT = -100123
TEACHER = "Иванов И.И."


def parsed(subject="Математика"):
    lessons = (Lesson(DAY, "ИС-22", 1, "1 пара", 1, "09:00", "10:30", subject, TEACHER, "24",
                      raw_data={"private-source": "not-for-the-outbox"}),)
    return ParsedSchedule("26.10.xlsx", DAY, DAY, (DAY,), lessons)


@pytest.fixture
def repository(tmp_path):
    repo = ScheduleRepository(tmp_path / "media.db")
    repo.save_profile(123, role="student", target="ИС-22")
    repo.save_profile(124, role="teacher", target=TEACHER)
    repo.save_chat_profile(CHAT, chat_type="supergroup", chat_title="ИС-22", target="ИС-22",
                           configured_by=123, pin_enabled=True)
    return repo


@pytest_asyncio.fixture
async def renderer(monkeypatch):
    service = CardService()
    monkeypatch.setattr(schedule_media, "get_card_service", lambda: service)
    monkeypatch.setattr(cards, "get_card_service", lambda: service)
    try:
        yield service
    finally:
        await service.aclose()


def bot():
    return SimpleNamespace(
        id=999, send_photo=AsyncMock(return_value=SimpleNamespace(message_id=501)),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=502)),
        get_chat_member=AsyncMock(return_value=SimpleNamespace(status=ChatMemberStatus.ADMINISTRATOR, can_pin_messages=True)),
        pin_chat_message=AsyncMock(), unpin_chat_message=AsyncMock(), edit_message_media=AsyncMock(),
        edit_message_text=AsyncMock(),
    )


def notifier(fake_bot, repo, *, clock=None):
    return ScheduleNotifier(fake_bot, repo, (), broadcast_interval=0, private_interval=0,
                            group_interval=0, clock=clock)


@pytest.mark.asyncio
async def test_new_publication_delivers_real_cards_to_students_teachers_and_chat(repository, renderer):
    result = repository.import_schedule(parsed(), sha256="initial")
    pending = repository.pending_notifications()
    assert len(pending) == 3
    assert all("raw_data" not in row["card_payload"]["lessons"][0] for row in pending)
    fake_bot = bot()
    await notifier(fake_bot, repository).notify_import(result)
    assert fake_bot.send_photo.await_count == 3
    fake_bot.send_message.assert_not_called()
    for sent in fake_bot.send_photo.call_args_list:
        with Image.open(BytesIO(sent.args[1].data)) as image:
            assert image.mode == "RGB" and image.width == 1080
        assert sent.kwargs["caption"] == "Опубликовано новое расписание"
        button = sent.kwargs["reply_markup"].inline_keyboard[0][0]
        expected = f"gnav:0:view_text_{DAY}" if sent.args[0] == CHAT else f"pnav:{sent.args[0]}:view_text_{DAY}"
        assert button.callback_data == expected
    assert repository.notification_outbox_counts() == {"sent": 3}


@pytest.mark.asyncio
async def test_card_retry_uses_persisted_import_snapshot_after_schedule_changes(repository, monkeypatch):
    repository.import_schedule(parsed(), sha256="initial")
    service = SimpleNamespace(get_card=AsyncMock(return_value=b"png"))
    monkeypatch.setattr(schedule_media, "get_card_service", lambda: service)
    fake_bot = bot()
    fake_bot.send_photo.side_effect = [TimeoutError("private-secret"), SimpleNamespace(message_id=501)]
    clock = [datetime.now(timezone.utc)]
    worker = notifier(fake_bot, repository, clock=lambda: clock[0])
    assert await worker.deliver_pending(limit=1) == 0
    first = repository.pending_notifications(now=clock[0] + timedelta(days=1))[0]
    repository.import_schedule(parsed("Физика"), sha256="updated")
    clock[0] = datetime.fromisoformat(first["next_attempt_at"]) + timedelta(seconds=1)
    reloaded = ScheduleRepository(repository.database_path)
    assert await notifier(fake_bot, reloaded, clock=lambda: clock[0]).deliver_pending(limit=1) == 1
    assert fake_bot.send_photo.await_count == 2
    fake_bot.send_message.assert_not_called()
    assert all(call.kwargs["lessons"][0].subject == "Математика" for call in service.get_card.call_args_list)


@pytest.mark.asyncio
async def test_render_failure_uses_complete_text_and_preserves_retry_button(repository, monkeypatch):
    repository.import_schedule(parsed(), sha256="initial")
    monkeypatch.setattr(schedule_media, "get_card_service", lambda: SimpleNamespace(get_card=AsyncMock(side_effect=CardNotSupported())))
    fake_bot = bot()
    assert await notifier(fake_bot, repository).deliver_pending() == 3
    fake_bot.send_photo.assert_not_called()
    assert fake_bot.send_message.await_count == 3
    for sent in fake_bot.send_message.call_args_list:
        assert "Математика" in sent.args[1] and "Опубликовано новое расписание" in sent.args[1]
        assert sent.kwargs["reply_markup"].inline_keyboard[0][0].text == "Карточка"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["modified", "added", "removed", "moved"])
async def test_change_notification_delivers_real_before_after_cards(repository, renderer, kind):
    original = parsed()
    repository.import_schedule(original, sha256="initial")
    for row in repository.pending_notifications():
        repository.mark_notification_sent(row["id"])
    lesson = original.lessons[0]
    added = replace(lesson, position=2, lesson_number=2, lesson_label="2 пара", subject="Физика")
    updated = {
        "modified": parsed("Физика"),
        "added": replace(original, lessons=(lesson, added)),
        "removed": replace(original, lessons=(), confirmed_empty_dates=(DAY,), date_groups={DAY: ("ИС-22",)}),
        "moved": replace(original, lessons=(replace(lesson, position=2, lesson_number=2,
                                                      lesson_label="2 пара", start_time="10:40", end_time="12:10"),)),
    }[kind]
    result = repository.import_schedule(updated, sha256="updated")
    pending = repository.pending_notifications()
    assert len(pending) == 3
    for row in pending:
        change = row["card_payload"]["changes"][0]
        assert row["card_payload"]["kind"] == "changes" and change["kind"] == kind
        assert all("raw_data" not in value for side in ("before", "after")
                   if (value := change[side]) is not None)
    fake_bot = bot()
    await notifier(fake_bot, repository).notify_import(result)
    assert fake_bot.send_photo.await_count == 3
    fake_bot.send_message.assert_not_called()
    for sent in fake_bot.send_photo.call_args_list:
        assert sent.kwargs["caption"] == "Расписание изменилось"
        with Image.open(BytesIO(sent.args[1].data)) as image:
            assert image.mode == "RGB" and image.width == 1080
    assert repository.notification_outbox_counts() == {"sent": 6}


@pytest.mark.asyncio
async def test_change_card_failure_preserves_complete_text(repository, monkeypatch):
    repository.import_schedule(parsed(), sha256="initial")
    for row in repository.pending_notifications():
        repository.mark_notification_sent(row["id"])
    repository.import_schedule(parsed("Физика"), sha256="updated")
    monkeypatch.setattr(schedule_media, "get_card_service", lambda: SimpleNamespace(
        get_change_card=AsyncMock(side_effect=CardNotSupported())))
    fake_bot = bot()
    assert await notifier(fake_bot, repository).deliver_pending() == 3
    fake_bot.send_photo.assert_not_called()
    assert fake_bot.send_message.await_count == 3
    assert all("Физика" in sent.args[1] and "Математика" in sent.args[1]
               and "Расписание изменилось" in sent.args[1] for sent in fake_bot.send_message.call_args_list)


@pytest.mark.asyncio
async def test_change_card_retry_preserves_both_sides_after_another_import(repository, monkeypatch):
    repository.import_schedule(parsed(), sha256="initial")
    for row in repository.pending_notifications():
        repository.mark_notification_sent(row["id"])
    repository.import_schedule(parsed("Физика"), sha256="changed")
    service = SimpleNamespace(get_change_card=AsyncMock(return_value=b"png"))
    monkeypatch.setattr(schedule_media, "get_card_service", lambda: service)
    fake_bot = bot()
    fake_bot.send_photo.side_effect = [TimeoutError("uncertain"), SimpleNamespace(message_id=503)]
    clock = [datetime.now(timezone.utc)]
    worker = notifier(fake_bot, repository, clock=lambda: clock[0])
    assert await worker.deliver_pending(limit=1) == 0
    first = repository.pending_notifications(now=clock[0] + timedelta(days=1))[0]
    repository.import_schedule(parsed("Химия"), sha256="newer")
    clock[0] = datetime.fromisoformat(first["next_attempt_at"]) + timedelta(seconds=1)
    reloaded = ScheduleRepository(repository.database_path)
    assert await notifier(fake_bot, reloaded, clock=lambda: clock[0]).deliver_pending(limit=1) == 1
    assert fake_bot.send_photo.await_count == 2
    fake_bot.send_message.assert_not_called()
    for call in service.get_change_card.call_args_list:
        change = call.kwargs["changes"][0]
        assert change.before.subject == "Математика" and change.after.subject == "Физика"


@pytest.mark.asyncio
@pytest.mark.parametrize("render_fails", [False, True])
async def test_change_card_covers_only_its_own_fallback_chunks(repository, monkeypatch, render_fails):
    repository.import_schedule(parsed(), sha256="initial")
    for row in repository.pending_notifications():
        repository.mark_notification_sent(row["id"])
    repository.set_notifications(124, False)
    repository.set_chat_notifications(CHAT, False)
    source = parsed("Очень длинный предмет " * 300 + "КОНЕЦ")
    next_day = DAY + timedelta(days=1)
    source = replace(source, end_date=next_day, dates=(DAY, next_day),
                     lessons=source.lessons + (replace(parsed().lessons[0], lesson_date=next_day),))
    repository.import_schedule(source, sha256="long-change-and-new-day")
    head = repository.pending_notifications()[0]
    following = head["card_payload"]["following_chunks"]
    assert following > 0
    service = SimpleNamespace(get_change_card=AsyncMock(
        side_effect=CardNotSupported() if render_fails else None, return_value=b"png"))
    monkeypatch.setattr(schedule_media, "get_card_service", lambda: service)
    fake_bot = bot()
    worker = notifier(fake_bot, repository)
    assert await worker.deliver_pending(limit=1) == 1
    if render_fails:
        fake_bot.send_photo.assert_not_called()
        assert repository.notification_outbox_counts()["sent"] == 4
        await worker.deliver_pending(limit=following)
        texts = [call.args[1] for call in fake_bot.send_message.call_args_list]
        assert "КОНЕЦ" in "".join(texts)
    else:
        fake_bot.send_photo.assert_awaited_once()
        fake_bot.send_message.assert_not_called()
        assert repository.notification_outbox_counts()["sent"] == 3 + following + 1
    next_head = next(row for row in repository.pending_notifications()
                     if row["destination_id"] == head["destination_id"])
    assert next_head["card_payload"]["date"] == next_day.isoformat()


@pytest.mark.asyncio
async def test_pinned_schedule_starts_as_photo_and_edits_media_for_same_day(repository, renderer):
    repository.import_schedule(parsed(), sha256="initial")
    fake_bot = bot()
    assert await sync_chat_schedule_pin(fake_bot, repository, chat_id=CHAT, target="ИС-22", now=NOW)
    fake_bot.send_photo.assert_awaited_once()
    fake_bot.send_message.assert_not_called()
    assert "reply_markup" not in fake_bot.send_photo.call_args.kwargs
    repository.import_schedule(parsed("Физика"), sha256="updated")
    assert await sync_chat_schedule_pin(fake_bot, repository, chat_id=CHAT, target="ИС-22", now=NOW)
    fake_bot.edit_message_media.assert_awaited_once()
    assert fake_bot.edit_message_media.call_args.kwargs["message_id"] == 501
    fake_bot.edit_message_text.assert_not_called()
    assert repository.get_chat_profile(CHAT)["last_pinned_message_id"] == 501
    assert await sync_chat_schedule_pin(fake_bot, repository, chat_id=CHAT, target="ИС-22", now=NOW)
    fake_bot.edit_message_media.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_photo_pin_is_replaced_and_new_photo_is_pinned_first(repository, renderer):
    repository.import_schedule(parsed(), sha256="initial")
    fake_bot = bot()
    await sync_chat_schedule_pin(fake_bot, repository, chat_id=CHAT, target="ИС-22", now=NOW)
    repository.import_schedule(parsed("Физика"), sha256="updated")
    fake_bot.edit_message_media.side_effect = TelegramBadRequest(
        method=EditMessageMedia(media={"type": "photo", "media": "test"}), message="message to edit not found",
    )
    events = []
    fake_bot.send_photo.return_value = SimpleNamespace(message_id=503)
    fake_bot.pin_chat_message.side_effect = lambda **kwargs: events.append(("pin", kwargs["message_id"]))
    fake_bot.unpin_chat_message.side_effect = lambda chat_id, **kwargs: events.append(("unpin", kwargs["message_id"]))
    assert await sync_chat_schedule_pin(fake_bot, repository, chat_id=CHAT, target="ИС-22", now=NOW)
    assert events == [("pin", 503), ("unpin", 501)]


@pytest.mark.asyncio
async def test_shared_publication_button_opens_owner_bound_text(repository, renderer):
    repository.import_schedule(parsed(), sha256="initial")
    message = SimpleNamespace(chat=SimpleNamespace(id=CHAT, type="supergroup"),
                              answer=AsyncMock(), answer_photo=AsyncMock(), delete=AsyncMock())
    callback = SimpleNamespace(data=f"gnav:0:view_text_{DAY}", from_user=SimpleNamespace(id=456),
                               message=message, answer=AsyncMock())
    await groups.group_menu_action(callback, None, repository, SimpleNamespace(timezone="Europe/Moscow"), SimpleNamespace(track=AsyncMock()))
    assert message.answer.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data == f"gnav:456:view_card_{DAY}"
    message.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_teacher_card_preserves_parallel_groups_and_linked_subgroup_projection(renderer):
    lessons = (
        Lesson(DAY, "ИС-22", 1, "1 пара", 1, "09:00", "10:30", "Математика", TEACHER, "24"),
        Lesson(DAY, "ИС-23", 1, "1 пара", 1, "09:00", "10:30", "Право", TEACHER, "25"),
    )
    data = await renderer.get_card(role="teacher", target=TEACHER, lesson_date=DAY, lessons=lessons)
    with Image.open(BytesIO(data)) as image:
        assert image.size == (1080, 860)
    split = replace(lessons[0], segments=(LessonSegment("Математика", TEACHER, "24", "1"),
                                         LessonSegment("Физика", "Петров П.П.", "25", "2")))
    with pytest.raises(CardNotSupported):
        await renderer.get_card(role="teacher", target=TEACHER, lesson_date=DAY, lessons=[split])
    assert await renderer.get_card(role="teacher", target=TEACHER, lesson_date=DAY, lessons=[split.for_teacher(TEACHER)])


@pytest.mark.asyncio
async def test_one_photo_atomically_covers_fallback_chunks_without_skipping_next_day(repository, monkeypatch):
    source = parsed("Очень длинный предмет " * 300)
    next_day = DAY + timedelta(days=1)
    source = replace(source, end_date=next_day, dates=(DAY, next_day),
                     lessons=source.lessons + (replace(parsed().lessons[0], lesson_date=next_day),))
    repository.import_schedule(source, sha256="long-and-next-day")
    head = repository.pending_notifications()[0]
    following = head["card_payload"]["following_chunks"]
    assert following > 0
    monkeypatch.setattr(schedule_media, "get_card_service", lambda: SimpleNamespace(get_card=AsyncMock(return_value=b"png")))
    fake_bot = bot()
    assert await notifier(fake_bot, repository).deliver_pending(limit=1) == 1
    fake_bot.send_photo.assert_awaited_once()
    fake_bot.send_message.assert_not_called()
    assert repository.notification_outbox_counts()["sent"] == following + 1
    next_head = next(row for row in repository.pending_notifications() if row["destination_id"] == head["destination_id"])
    assert next_head["card_payload"]["date"] == next_day.isoformat()


@pytest.mark.asyncio
async def test_long_notification_fallback_delivers_all_chunks_and_button_only_on_last(repository, monkeypatch):
    repository.import_schedule(parsed("Очень длинный предмет " * 300 + "КОНЕЦ"), sha256="long-fallback")
    monkeypatch.setattr(schedule_media, "get_card_service", lambda: SimpleNamespace(get_card=AsyncMock(side_effect=CardNotSupported())))
    fake_bot = bot()
    await notifier(fake_bot, repository).deliver_pending()
    fake_bot.send_photo.assert_not_called()
    for recipient in (123, 124, CHAT):
        sent = [call for call in fake_bot.send_message.call_args_list if call.args[0] == recipient]
        assert len(sent) > 1
        assert "КОНЕЦ" in "".join(call.args[1] for call in sent)
        assert all(call.kwargs["reply_markup"] is None for call in sent[:-1])
        assert sent[-1].kwargs["reply_markup"].inline_keyboard[0][0].text == "Карточка"
    assert set(repository.notification_outbox_counts()) == {"sent"}


def test_media_schema_migration_preserves_legacy_queue_and_rolls_back_on_failure(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    legacy = ScheduleRepository(path)
    legacy.SCHEMA_VERSION = 10
    legacy.initialize()
    timestamp = NOW.isoformat()
    with legacy._connect() as connection:
        connection.execute(
            "INSERT INTO imports(id,source_filename,sha256,imported_at,start_date,end_date) VALUES(1,'old.xlsx','old',?,?,?)",
            (timestamp, DAY.isoformat(), DAY.isoformat()),
        )
        connection.execute(
            "INSERT INTO notification_outbox(import_id,destination_id,destination_kind,chat_type,chunk_index,text,created_at,next_attempt_at,expires_at) VALUES(1,123,'user','private',0,'legacy text',?,?,?)",
            (timestamp, timestamp, (NOW + timedelta(days=7)).isoformat()),
        )
        connection.commit()
    latest = ScheduleRepository(path)
    migrate = latest._migrate_to_v11

    def fail(connection):
        migrate(connection)
        raise RuntimeError("injected")

    monkeypatch.setattr(latest, "_migrate_to_v11", fail)
    with pytest.raises(RuntimeError):
        latest.initialize()
    assert latest.current_schema_version() == 10
    with latest._connect() as connection:
        assert "card_payload" not in {row[1] for row in connection.execute("PRAGMA table_info(notification_outbox)")}
    monkeypatch.setattr(latest, "_migrate_to_v11", migrate)
    latest.initialize()
    with latest._connect() as connection:
        row = connection.execute("SELECT text,card_payload FROM notification_outbox").fetchone()
        assert row["text"] == "legacy text" and row["card_payload"] is None
