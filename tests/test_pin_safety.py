from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.usefixtures("text_schedule_media")
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import EditMessageText, PinChatMessage, UnpinChatMessage

from schedule_bot.bot.groups import save_group_setup
from schedule_bot.domain import Lesson, ParsedSchedule
from schedule_bot.pinning import (
    pin_schedule_message,
    schedule_for_pin,
    sync_chat_schedule_pin,
    unpin_schedule_messages,
)
from schedule_bot.repository import ScheduleRepository


CHAT_ID = -100123
TODAY = date(2026, 10, 13)
NOW = datetime(2026, 10, 13, 9, 0)


class PinBot:
    id = 999

    def __init__(self) -> None:
        self.allowed = True
        self.pin_error: Exception | None = None
        self.unpin_error: Exception | None = None
        self.edit_error: Exception | None = None
        self.sent: list[tuple[int, str]] = []
        self.edited: list[tuple[int, int, str]] = []
        self.actions: list[tuple[str, int]] = []
        self.permission_checks = 0

    async def get_chat_member(self, chat_id: int, user_id: int) -> SimpleNamespace:
        self.permission_checks += 1
        return SimpleNamespace(
            status=ChatMemberStatus.ADMINISTRATOR,
            can_pin_messages=self.allowed,
        )

    async def send_message(self, chat_id: int, text: str, **kwargs) -> SimpleNamespace:
        await asyncio.sleep(0)
        self.sent.append((chat_id, text))
        message_id = 100 + len(self.sent)
        self.actions.append(("send", message_id))
        return SimpleNamespace(message_id=message_id)

    async def edit_message_text(
        self, text: str, *, chat_id: int, message_id: int
    ) -> None:
        self.edited.append((chat_id, message_id, text))
        if self.edit_error:
            raise self.edit_error

    async def pin_chat_message(
        self, *, chat_id: int, message_id: int, disable_notification: bool
    ) -> None:
        self.actions.append(("pin", message_id))
        if self.pin_error:
            raise self.pin_error

    async def unpin_chat_message(self, chat_id: int, *, message_id: int) -> None:
        self.actions.append(("unpin", message_id))
        if self.unpin_error:
            raise self.unpin_error


def _lesson(lesson_date: date = TODAY, group: str = "ИС-22") -> Lesson:
    return Lesson(
        lesson_date=lesson_date,
        group_name=group,
        position=1,
        lesson_label="1 пара",
        lesson_number=1,
        start_time="09:00",
        end_time="10:30",
        subject="Математика",
        teacher="Иванов И.И.",
        room="41",
    )


def _parsed(*lessons: Lesson) -> ParsedSchedule:
    dates = tuple(sorted({lesson.lesson_date for lesson in lessons}))
    return ParsedSchedule(
        source_filename="pin-safety.xlsx",
        start_date=dates[0],
        end_date=dates[-1],
        dates=dates,
        lessons=tuple(lessons),
    )


def _repository(tmp_path: Path, *lessons: Lesson) -> ScheduleRepository:
    repository = ScheduleRepository(tmp_path / "schedule.db")
    repository.import_schedule(_parsed(*(lessons or (_lesson(),))), sha256="initial")
    repository.save_chat_profile(
        CHAT_ID,
        chat_type="supergroup",
        chat_title="ИС-22",
        target="ИС-22",
        configured_by=123,
        pin_enabled=True,
    )
    repository.set_chat_last_pin(
        CHAT_ID,
        message_id=10,
        schedule_date=TODAY - timedelta(days=1),
        schedule_hash="old-pin",
    )
    return repository


async def _sync(bot: PinBot, repository: ScheduleRepository, *, now=NOW):
    return await sync_chat_schedule_pin(
        bot, repository, chat_id=CHAT_ID, target="ИС-22", now=now
    )


@pytest.mark.parametrize("when", [NOW, NOW.replace(hour=11)])
def test_no_archive_fallback_for_pin(tmp_path: Path, when: datetime) -> None:
    repository = _repository(tmp_path, _lesson(TODAY - timedelta(days=1)))
    assert repository.available_dates(from_date=TODAY) == [TODAY - timedelta(days=1)]
    assert schedule_for_pin(repository, target="ИС-22", now=when) is None


@pytest.mark.asyncio
async def test_finished_today_without_future_preserves_old_pin(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    bot = PinBot()
    assert await _sync(bot, repository, now=NOW.replace(hour=11)) is None
    assert bot.actions == []
    assert repository.get_chat_profile(CHAT_ID)["last_pinned_message_id"] == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [TelegramBadRequest, TelegramForbiddenError])
async def test_failed_new_pin_reuses_candidate_across_restart(
    tmp_path: Path, error_type: type
) -> None:
    repository = _repository(tmp_path)
    bot = PinBot()
    bot.pin_error = error_type(
        method=PinChatMessage(chat_id=CHAT_ID, message_id=101),
        message="not enough rights to pin a message",
    )
    assert await _sync(bot, repository) is False
    state = repository.get_chat_profile(CHAT_ID)
    assert state["last_pinned_message_id"] == 10
    assert state["pending_pin_message_id"] == 101
    assert state["pin_attempts"] == 1
    assert not any(action == "unpin" for action, _ in bot.actions)

    restarted = ScheduleRepository(repository.database_path)
    assert await _sync(bot, restarted, now=NOW + timedelta(minutes=1)) is False
    assert await _sync(bot, restarted, now=NOW + timedelta(minutes=5)) is False
    assert len(bot.sent) == 1
    assert bot.actions == [("send", 101), ("pin", 101), ("pin", 101)]
    assert restarted.get_chat_profile(CHAT_ID)["last_pinned_message_id"] == 10

    bot.pin_error = None
    assert await _sync(bot, restarted, now=NOW + timedelta(minutes=15)) is True
    assert len(bot.sent) == 1
    assert bot.actions[-2:] == [("pin", 101), ("unpin", 10)]
    state = restarted.get_chat_profile(CHAT_ID)
    assert state["last_pinned_message_id"] == 101
    assert state["pending_pin_message_id"] is None


@pytest.mark.asyncio
async def test_missing_permission_never_publishes_schedule(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    bot = PinBot()
    bot.allowed = False
    assert await _sync(bot, repository) is False
    assert await _sync(bot, repository, now=NOW + timedelta(minutes=1)) is False
    assert bot.sent == []
    assert bot.permission_checks == 1
    assert repository.get_chat_profile(CHAT_ID)["last_pinned_message_id"] == 10

    bot.allowed = True
    assert await _sync(bot, repository, now=NOW + timedelta(minutes=5)) is True
    assert bot.actions == [("send", 101), ("pin", 101), ("unpin", 10)]


@pytest.mark.asyncio
async def test_restored_permission_pins_existing_candidate(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    bot = PinBot()
    bot.pin_error = TelegramForbiddenError(
        method=PinChatMessage(chat_id=CHAT_ID, message_id=101), message="Forbidden"
    )
    assert await _sync(bot, repository) is False
    bot.pin_error = None
    bot.allowed = False
    assert await _sync(bot, repository, now=NOW + timedelta(minutes=5)) is False
    assert len(bot.sent) == 1
    bot.allowed = True
    assert await _sync(bot, repository, now=NOW + timedelta(minutes=15)) is True
    assert len(bot.sent) == 1
    assert repository.get_chat_profile(CHAT_ID)["last_pinned_message_id"] == 101


@pytest.mark.asyncio
async def test_direct_pin_happens_before_old_unpin(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    bot = PinBot()
    assert await pin_schedule_message(
        bot, repository, chat_id=CHAT_ID, message_id=20, now=NOW
    ) is True
    assert bot.actions == [("pin", 20), ("unpin", 10)]


@pytest.mark.asyncio
async def test_old_unpin_failure_keeps_both_messages_tracked(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    bot = PinBot()
    bot.unpin_error = TelegramForbiddenError(
        method=UnpinChatMessage(chat_id=CHAT_ID, message_id=10), message="Forbidden"
    )
    assert await _sync(bot, repository) is False
    state = repository.get_chat_profile(CHAT_ID)
    assert state["last_pinned_message_id"] == 10
    assert state["pending_pin_message_id"] == 101

    bot.unpin_error = None
    restarted = ScheduleRepository(repository.database_path)
    assert await _sync(bot, restarted, now=NOW + timedelta(minutes=5)) is True
    assert len(bot.sent) == 1
    assert restarted.get_chat_profile(CHAT_ID)["last_pinned_message_id"] == 101


@pytest.mark.asyncio
async def test_disable_unpins_confirmed_and_pending_messages(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.set_chat_pending_pin(
        CHAT_ID, message_id=101, schedule_date=TODAY, schedule_hash="pending"
    )
    bot = PinBot()
    assert await unpin_schedule_messages(bot, repository, chat_id=CHAT_ID) is True
    assert bot.actions == [("unpin", 10), ("unpin", 101)]
    state = repository.get_chat_profile(CHAT_ID)
    assert state["last_pinned_message_id"] is None
    assert state["pending_pin_message_id"] is None


@pytest.mark.asyncio
async def test_changed_schedule_edits_pending_candidate_instead_of_sending_again(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    bot = PinBot()
    bot.pin_error = TelegramBadRequest(
        method=PinChatMessage(chat_id=CHAT_ID, message_id=101), message="no pin rights"
    )
    assert await _sync(bot, repository) is False
    repository.import_schedule(
        _parsed(replace(_lesson(), subject="Физика")), sha256="changed"
    )
    bot.pin_error = None
    assert await _sync(bot, repository, now=NOW + timedelta(minutes=5)) is True
    assert len(bot.sent) == 1
    assert len(bot.edited) == 1
    assert bot.edited[0][:2] == (CHAT_ID, 101)
    assert "Физика" in bot.edited[0][2]


@pytest.mark.asyncio
async def test_deleted_pending_candidate_allows_one_replacement(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    bot = PinBot()
    bot.pin_error = TelegramBadRequest(
        method=PinChatMessage(chat_id=CHAT_ID, message_id=101), message="no pin rights"
    )
    assert await _sync(bot, repository) is False
    bot.pin_error = TelegramBadRequest(
        method=PinChatMessage(chat_id=CHAT_ID, message_id=101),
        message="message to pin not found",
    )
    assert await _sync(bot, repository, now=NOW + timedelta(minutes=5)) is False
    state = repository.get_chat_profile(CHAT_ID)
    assert state["last_pinned_message_id"] == 10
    assert state["pending_pin_message_id"] is None

    bot.pin_error = None
    assert await _sync(bot, repository, now=NOW + timedelta(minutes=10)) is True
    assert len(bot.sent) == 2
    assert repository.get_chat_profile(CHAT_ID)["last_pinned_message_id"] == 102


@pytest.mark.asyncio
@pytest.mark.parametrize("can_retire_candidate", [True, False])
async def test_uneditable_pending_candidate_is_retired_before_replacement(
    tmp_path: Path, can_retire_candidate: bool
) -> None:
    repository = _repository(tmp_path)
    bot = PinBot()
    bot.pin_error = TelegramBadRequest(
        method=PinChatMessage(chat_id=CHAT_ID, message_id=101), message="no pin rights"
    )
    assert await _sync(bot, repository) is False
    repository.import_schedule(
        _parsed(replace(_lesson(), subject="Физика")), sha256="changed"
    )
    bot.pin_error = None
    bot.edit_error = TelegramBadRequest(
        method=EditMessageText(chat_id=CHAT_ID, message_id=101, text="new schedule"),
        message="message can't be edited",
    )
    if not can_retire_candidate:
        bot.unpin_error = TelegramForbiddenError(
            method=UnpinChatMessage(chat_id=CHAT_ID, message_id=101),
            message="Forbidden",
        )
        assert await _sync(bot, repository, now=NOW + timedelta(minutes=5)) is False
        assert await _sync(bot, repository, now=NOW + timedelta(minutes=6)) is False
        state = repository.get_chat_profile(CHAT_ID)
        assert state["last_pinned_message_id"] == 10
        assert state["pending_pin_message_id"] == 101
        assert len(bot.sent) == 1
        bot.unpin_error = None
    retry_minutes = 5 if can_retire_candidate else 15
    assert await _sync(bot, repository, now=NOW + timedelta(minutes=retry_minutes)) is True
    assert bot.actions[-4:] == [
        ("unpin", 101), ("send", 102), ("pin", 102), ("unpin", 10)
    ]
    assert repository.get_chat_profile(CHAT_ID)["last_pinned_message_id"] == 102


@pytest.mark.asyncio
async def test_concurrent_syncs_share_lock_across_repository_instances(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    restarted = ScheduleRepository(repository.database_path)
    bot = PinBot()
    results = await asyncio.gather(_sync(bot, repository), _sync(bot, restarted))
    assert results == [True, True]
    assert len(bot.sent) == 1
    assert bot.actions == [("send", 101), ("pin", 101), ("unpin", 10)]


@pytest.mark.asyncio
@pytest.mark.parametrize("has_future_schedule", [True, False])
async def test_changing_group_keeps_old_pin_until_replacement_is_pinned(
    tmp_path: Path, monkeypatch, has_future_schedule: bool
) -> None:
    monkeypatch.setattr("schedule_bot.bot.groups._now", lambda settings: NOW)
    second_date = TODAY if has_future_schedule else TODAY - timedelta(days=1)
    repository = _repository(tmp_path, _lesson(), _lesson(second_date, group="ПД-12"))
    bot = PinBot()
    bot.pin_error = TelegramForbiddenError(
        method=PinChatMessage(chat_id=CHAT_ID, message_id=101), message="Forbidden"
    )

    async def no_op(*args, **kwargs):
        return None

    callback = SimpleNamespace(
        data="group_setup:123:ПД-12",
        from_user=SimpleNamespace(id=123),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=CHAT_ID, type="supergroup", title="Группа"),
            edit_text=no_op,
        ),
        answer=no_op,
    )
    await save_group_setup(
        callback,
        bot=bot,
        repository=repository,
        settings=SimpleNamespace(timezone="Europe/Moscow"),
        analytics=SimpleNamespace(track=no_op),
    )
    state = repository.get_chat_profile(CHAT_ID)
    assert state["target"] == "ПД-12"
    assert state["last_pinned_message_id"] == 10
    assert not any(action == "unpin" for action, _ in bot.actions)
    if has_future_schedule:
        assert len(bot.sent) == 1
        bot.pin_error = None
        restarted = ScheduleRepository(repository.database_path)
        # The target argument may have been read by an automatic job before /setup.
        assert await _sync(bot, restarted, now=NOW + timedelta(minutes=5)) is True
        assert len(bot.sent) == 1
        assert bot.actions[-2:] == [("pin", 101), ("unpin", 10)]
    else:
        assert bot.sent == []
        assert await _sync(bot, repository) is None
