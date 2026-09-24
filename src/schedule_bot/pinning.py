from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime, time, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from schedule_bot.domain import Lesson
from schedule_bot.presentation import format_schedule
from schedule_bot.repository import ScheduleRepository


logger = logging.getLogger(__name__)


async def pin_schedule_message(
    bot: Bot,
    repository: ScheduleRepository,
    *,
    chat_id: int,
    message_id: int,
    schedule_date: date | None = None,
    schedule_hash: str | None = None,
) -> bool:
    """Replace only the schedule pin previously created by this bot."""
    profile = repository.get_chat_profile(chat_id)
    if profile is None or not bool(profile["pin_enabled"]):
        return False
    previous_id = profile.get("last_pinned_message_id")
    try:
        if previous_id and int(previous_id) != message_id:
            try:
                await bot.unpin_chat_message(
                    chat_id,
                    message_id=int(previous_id),
                )
            except TelegramBadRequest:
                logger.info("Previous bot pin %s is no longer available", previous_id)
        await bot.pin_chat_message(
            chat_id=chat_id,
            message_id=message_id,
            disable_notification=True,
        )
    except (TelegramBadRequest, TelegramForbiddenError):
        logger.exception("Could not pin schedule in chat %s", chat_id)
        return False
    repository.set_chat_last_pin(
        chat_id,
        message_id=message_id,
        schedule_date=schedule_date,
        schedule_hash=schedule_hash,
    )
    return True


def schedule_for_pin(
    repository: ScheduleRepository,
    *,
    target: str,
    now: datetime,
) -> tuple[date, list[Lesson]] | None:
    """Choose today's active schedule or the nearest future study day."""
    today = now.date()
    today_lessons = repository.lessons_for(
        role="student",
        target=target,
        lesson_date=today,
    )
    if today_lessons and not _all_lessons_finished(today_lessons, now):
        return today, today_lessons

    first_candidate = today + timedelta(days=1) if today_lessons else today
    for lesson_date in repository.available_dates(from_date=first_candidate):
        lessons = repository.lessons_for(
            role="student",
            target=target,
            lesson_date=lesson_date,
        )
        if lessons:
            return lesson_date, lessons
    return None


def _all_lessons_finished(lessons: list[Lesson], now: datetime) -> bool:
    end_times: list[time] = []
    for lesson in lessons:
        if not lesson.end_time:
            return False
        try:
            end_times.append(time.fromisoformat(lesson.end_time))
        except ValueError:
            return False
    local_time = now.time().replace(tzinfo=None)
    return bool(end_times) and local_time >= max(end_times)


def _schedule_hash(target: str, lessons: list[Lesson]) -> str:
    payload = {
        "target": target,
        "lessons": [lesson.to_dict() for lesson in lessons],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def sync_chat_schedule_pin(
    bot: Bot,
    repository: ScheduleRepository,
    *,
    chat_id: int,
    target: str,
    now: datetime,
) -> bool | None:
    """Ensure a chat pins the schedule relevant at the given local time."""
    profile = repository.get_chat_profile(chat_id)
    if profile is None or not bool(profile["pin_enabled"]):
        return False

    selected = schedule_for_pin(repository, target=target, now=now)
    if selected is None:
        return None
    lesson_date, lessons = selected
    schedule_hash = _schedule_hash(target, lessons)
    if (
        profile.get("last_pinned_message_id") is not None
        and profile.get("last_pinned_schedule_date") == lesson_date
        and profile.get("last_pinned_schedule_hash") == schedule_hash
    ):
        return True

    message_text = format_schedule(lessons, target, lesson_date, "student")
    previous_id = profile.get("last_pinned_message_id")
    if previous_id is not None and profile.get("last_pinned_schedule_date") == lesson_date:
        try:
            await bot.edit_message_text(
                message_text,
                chat_id=chat_id,
                message_id=int(previous_id),
            )
        except TelegramBadRequest as error:
            if "message is not modified" in str(error).casefold():
                repository.set_chat_last_pin(
                    chat_id,
                    message_id=int(previous_id),
                    schedule_date=lesson_date,
                    schedule_hash=schedule_hash,
                )
                return True
            logger.info("Could not edit schedule pin %s; replacing it", previous_id)
        else:
            repository.set_chat_last_pin(
                chat_id,
                message_id=int(previous_id),
                schedule_date=lesson_date,
                schedule_hash=schedule_hash,
            )
            return True

    sent = await bot.send_message(
        chat_id,
        message_text,
    )
    return await pin_schedule_message(
        bot,
        repository,
        chat_id=chat_id,
        message_id=sent.message_id,
        schedule_date=lesson_date,
        schedule_hash=schedule_hash,
    )


async def sync_schedule_pins(
    bot: Bot,
    repository: ScheduleRepository,
    *,
    now: datetime,
) -> None:
    """Refresh all enabled group pins without one broken chat stopping others."""
    for profile in repository.pinned_chats():
        chat_id = int(profile["chat_id"])
        try:
            await sync_chat_schedule_pin(
                bot,
                repository,
                chat_id=chat_id,
                target=str(profile["target"]),
                now=now,
            )
        except (TelegramBadRequest, TelegramForbiddenError):
            logger.exception("Could not synchronize schedule pin in chat %s", chat_id)
        except Exception:
            logger.exception("Unexpected schedule pin error in chat %s", chat_id)


async def unpin_schedule_messages(
    bot: Bot,
    repository: ScheduleRepository,
    *,
    chat_id: int,
) -> bool:
    """Unpin every schedule message tracked by the bot without touching others."""
    profile = repository.get_chat_profile(chat_id)
    if profile is None:
        return True
    message_id = profile.get("last_pinned_message_id")
    if message_id is None:
        repository.set_chat_last_pin(
            chat_id,
            message_id=None,
            schedule_date=None,
            schedule_hash=None,
        )
        return True

    try:
        await bot.unpin_chat_message(
            chat_id,
            message_id=int(message_id),
        )
    except TelegramBadRequest as error:
        error_text = str(error).casefold()
        if not any(
            marker in error_text
            for marker in ("message to unpin not found", "message is not pinned")
        ):
            logger.exception("Could not unpin schedule in chat %s", chat_id)
            return False
        logger.info("Tracked schedule pin %s is already absent", message_id)
    except TelegramForbiddenError:
        logger.exception("Could not unpin schedule in chat %s", chat_id)
        return False

    repository.set_chat_last_pin(
        chat_id,
        message_id=None,
        schedule_date=None,
        schedule_hash=None,
    )
    return True
