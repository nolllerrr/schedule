from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Chat, Document, Message, Update, User

from schedule_bot import config
from schedule_bot.bot.app import automatic_update_job
from schedule_bot.bot.signals import ScheduleSignalListener, create_signal_router


SOURCE = "ptghautoraspisaniebot"


def message(**changes) -> Message:
    values = dict(
        message_id=10,
        date=datetime.now(timezone.utc),
        chat=Chat(id=-100123, type="supergroup"),
        from_user=User(id=222, is_bot=True, first_name="Source", username=SOURCE),
        text="Обновлено расписание",
    )
    values.update(changes)
    return Message(**values)


@pytest.fixture
def bot():
    return Bot("111:test")


@pytest.fixture
def listener():
    return ScheduleSignalListener(
        "@" + SOURCE.upper(), AsyncMock(return_value={"active": True}), AsyncMock(),
        chat_id=-100123,
        cooldown_seconds=0.03,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"from_user": None},
    {"from_user": User(id=222, is_bot=False, first_name="Human", username=SOURCE)},
    {"from_user": User(id=222, is_bot=True, first_name="Other", username="otherbot")},
    {"from_user": User(id=111, is_bot=True, first_name="Self", username=SOURCE)},
    {"chat": Chat(id=123, type="private")},
    {"chat": Chat(id=-100123, type="channel")},
    {"chat": Chat(id=-100999, type="supergroup")},
    {"text": None},
])
async def test_untrusted_messages_do_not_even_read_profiles(listener, bot, changes):
    await listener.receive(message(**changes), bot)
    listener.get_chat_profile.assert_not_awaited()
    assert not listener._pending.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", [None, {"active": False}])
async def test_signal_requires_an_active_configured_chat(listener, bot, profile):
    listener.get_chat_profile.return_value = profile
    await listener.receive(message(), bot)
    assert not listener._pending.is_set()


@pytest.mark.asyncio
async def test_dispatcher_receives_new_and_edited_messages_without_replies(listener, bot):
    dispatcher = Dispatcher()
    dispatcher.include_router(create_signal_router(listener))
    original = message()
    await dispatcher.feed_update(bot, Update(update_id=1, message=original))
    assert listener._pending.is_set()
    listener._pending.clear()
    # Redelivery and an edit that only changes metadata are duplicates.
    await dispatcher.feed_update(bot, Update(update_id=2, message=original))
    await dispatcher.feed_update(bot, Update(update_id=3, edited_message=original))
    assert not listener._pending.is_set()
    await dispatcher.feed_update(
        bot, Update(update_id=4, edited_message=message(text="Исправлено расписание"))
    )
    assert listener._pending.is_set()
    await bot.session.close()


@pytest.mark.asyncio
async def test_document_caption_and_replacement_are_signals(listener, bot):
    document = Document(file_id="file-a", file_unique_id="unique-a", file_name="a.xlsx")
    await listener.receive(message(text=None, caption="Расписание", document=document), bot)
    assert listener._pending.is_set()
    listener._pending.clear()
    await listener.receive(message(text=None, caption="Правка", document=document), bot)
    assert listener._pending.is_set()
    listener._pending.clear()
    replacement = document.model_copy(update={"file_unique_id": "unique-b"})
    await listener.receive(message(text=None, caption="Правка", document=replacement), bot)
    assert listener._pending.is_set()


@pytest.mark.asyncio
async def test_burst_is_coalesced_and_signal_during_check_is_not_lost(listener, bot):
    started = asyncio.Event()
    release = asyncio.Event()
    second = asyncio.Event()
    times = []

    async def check():
        times.append(asyncio.get_running_loop().time())
        if len(times) == 1:
            started.set()
            await release.wait()
        else:
            second.set()

    listener.update_job = check
    for index in range(5):
        await listener.receive(message(message_id=index), bot)
    worker = asyncio.create_task(listener.run())
    try:
        await asyncio.wait_for(started.wait(), 1)
        assert len(times) == 1
        for index in range(5, 10):
            await listener.receive(message(message_id=index), bot)
        released_at = asyncio.get_running_loop().time()
        release.set()
        await asyncio.wait_for(second.wait(), 1)
        assert len(times) == 2
        assert times[1] - released_at >= listener.cooldown_seconds
        assert not listener._pending.is_set()
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker


@pytest.mark.asyncio
async def test_worker_survives_failed_check_and_seen_cache_is_bounded(listener, bot):
    first = asyncio.Event()
    recovered = asyncio.Event()

    async def check():
        if not first.is_set():
            first.set()
            raise RuntimeError("test failure")
        recovered.set()

    listener.update_job = check
    for index in range(515):
        await listener.receive(message(message_id=index), bot)
    assert len(listener._seen) == 512
    worker = asyncio.create_task(listener.run())
    try:
        await asyncio.wait_for(first.wait(), 1)
        await listener.receive(message(message_id=516), bot)
        await asyncio.wait_for(recovered.wait(), 1)
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker


@pytest.mark.asyncio
async def test_signal_check_waits_for_existing_update_instead_of_skipping():
    lock = asyncio.Lock()
    await lock.acquire()
    result = SimpleNamespace(skipped=True, lesson_count=0, changes=[])
    updater = SimpleNamespace(update_once=AsyncMock(return_value=result))
    notifier = SimpleNamespace(notify_import=AsyncMock(), notify_admins_error=AsyncMock())
    analytics = SimpleNamespace(track=AsyncMock())
    pins = AsyncMock()
    # Timer checks retain their existing skip-when-busy behavior.
    await automatic_update_job(updater, notifier, analytics, lock, pins)
    updater.update_once.assert_not_awaited()
    waiting = asyncio.create_task(automatic_update_job(
        updater, notifier, analytics, lock, pins, wait_for_lock=True
    ))
    try:
        await asyncio.sleep(0)
        assert not waiting.done()
        updater.update_once.assert_not_awaited()
        lock.release()
        await asyncio.wait_for(waiting, 1)
        updater.update_once.assert_awaited_once_with(automatic=True)
        notifier.notify_import.assert_awaited_once_with(result)
        pins.assert_awaited_once()
    finally:
        if lock.locked():
            lock.release()
        waiting.cancel()
        with suppress(asyncio.CancelledError):
            await waiting


def test_signal_settings_are_opt_in_and_accept_the_supplied_username(monkeypatch):
    monkeypatch.setattr(config, "_load_dotenv", lambda: None)
    monkeypatch.delenv("SCHEDULE_SIGNAL_BOT_USERNAME", raising=False)
    monkeypatch.delenv("SCHEDULE_SIGNAL_CHAT_ID", raising=False)
    monkeypatch.delenv("SCHEDULE_SIGNAL_COOLDOWN_SECONDS", raising=False)
    settings = config.Settings.from_env(require_bot_token=False)
    assert settings.schedule_signal_bot_username == ""
    assert settings.schedule_signal_chat_id == 0
    assert settings.schedule_signal_cooldown_seconds == 60
    monkeypatch.setenv("SCHEDULE_SIGNAL_BOT_USERNAME", "@" + SOURCE)
    monkeypatch.setenv("SCHEDULE_SIGNAL_CHAT_ID", "-100123")
    monkeypatch.setenv("SCHEDULE_SIGNAL_COOLDOWN_SECONDS", "120")
    settings = config.Settings.from_env(require_bot_token=False)
    assert settings.schedule_signal_bot_username == "@" + SOURCE
    assert settings.schedule_signal_chat_id == -100123
    assert settings.schedule_signal_cooldown_seconds == 120


@pytest.mark.parametrize("name,value", [
    ("SCHEDULE_SIGNAL_BOT_USERNAME", "https://t.me/example"),
    ("SCHEDULE_SIGNAL_BOT_USERNAME", "bot with spaces"),
    ("SCHEDULE_SIGNAL_COOLDOWN_SECONDS", "0"),
    ("SCHEDULE_SIGNAL_COOLDOWN_SECONDS", "3601"),
    ("SCHEDULE_SIGNAL_COOLDOWN_SECONDS", "not-a-number"),
    ("SCHEDULE_SIGNAL_CHAT_ID", "123"),
    ("SCHEDULE_SIGNAL_CHAT_ID", "not-a-number"),
])
def test_invalid_signal_settings_fail_safely(monkeypatch, name, value):
    monkeypatch.setattr(config, "_load_dotenv", lambda: None)
    monkeypatch.delenv("SCHEDULE_SIGNAL_BOT_USERNAME", raising=False)
    monkeypatch.setenv("SCHEDULE_SIGNAL_CHAT_ID", "-100123")
    monkeypatch.delenv("SCHEDULE_SIGNAL_COOLDOWN_SECONDS", raising=False)
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        config.Settings.from_env(require_bot_token=False)


def test_enabled_signal_requires_a_specific_chat(monkeypatch):
    monkeypatch.setattr(config, "_load_dotenv", lambda: None)
    monkeypatch.setenv("SCHEDULE_SIGNAL_BOT_USERNAME", SOURCE)
    monkeypatch.delenv("SCHEDULE_SIGNAL_CHAT_ID", raising=False)
    with pytest.raises(ValueError, match="SCHEDULE_SIGNAL_CHAT_ID"):
        config.Settings.from_env(require_bot_token=False)
