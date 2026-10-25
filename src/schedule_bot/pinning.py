from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import date, datetime, timezone
from weakref import WeakKeyDictionary, WeakValueDictionary

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import InputMediaPhoto

from schedule_bot.domain import Lesson
from schedule_bot.presentation import format_schedule
from schedule_bot.repository import ScheduleRepository
from schedule_bot.async_repository import repository_async
from schedule_bot.freshness import freshness_notice
from schedule_bot.schedule_selection import current_schedule
from schedule_bot.schedule_media import schedule_photo


logger = logging.getLogger(__name__)
_pin_locks: WeakKeyDictionary[
    asyncio.AbstractEventLoop, WeakValueDictionary[tuple[str, int], asyncio.Lock]
] = WeakKeyDictionary()


def _chat_pin_lock(repository: ScheduleRepository, chat_id: int) -> asyncio.Lock:
    locks = _pin_locks.setdefault(asyncio.get_running_loop(), WeakValueDictionary())
    key = (str(repository.database_path.resolve()), chat_id)
    return locks.setdefault(key, asyncio.Lock())


def _utc_now(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


async def bot_can_pin(bot: Bot, chat_id: int) -> bool:
    member = await bot.get_chat_member(chat_id, bot.id)
    return member.status == ChatMemberStatus.CREATOR or (
        member.status == ChatMemberStatus.ADMINISTRATOR
        and bool(getattr(member, "can_pin_messages", False))
    )


def _message_is_absent(error: TelegramBadRequest, *, operation: str) -> bool:
    error_text = str(error).casefold()
    return f"message to {operation} not found" in error_text or (
        operation == "unpin" and "message is not pinned" in error_text
    )


def _cannot_edit(error: TelegramBadRequest) -> bool:
    text = str(error).casefold()
    return "message can't be edited" in text or "there is no text in the message to edit" in text


async def _edit_schedule_pin(bot: Bot, chat_id: int, message_id: int, text: str, photo: InputMediaPhoto | None) -> None:
    if photo is not None:
        await bot.edit_message_media(media=photo, chat_id=chat_id, message_id=message_id)
    else:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id)


async def _unpin_message(bot: Bot, chat_id: int, message_id: int) -> None:
    try:
        await bot.unpin_chat_message(chat_id, message_id=message_id)
    except TelegramBadRequest as error:
        if not _message_is_absent(error, operation="unpin"):
            raise
        logger.info("Tracked schedule pin %s is already absent", message_id)


async def pin_schedule_message(
    bot: Bot,
    repository: ScheduleRepository,
    *,
    chat_id: int,
    message_id: int,
    schedule_date: date | None = None,
    schedule_hash: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Replace only the schedule pin previously created by this bot."""
    async with _chat_pin_lock(repository, chat_id):
        return await _pin_schedule_message(
            bot,
            repository,
            chat_id=chat_id,
            message_id=message_id,
            schedule_date=schedule_date,
            schedule_hash=schedule_hash,
            now=now or datetime.now(timezone.utc),
        )


async def _pin_schedule_message(
    bot: Bot,
    repository: ScheduleRepository,
    *,
    chat_id: int,
    message_id: int,
    schedule_date: date | None,
    schedule_hash: str | None,
    now: datetime,
    permission_checked: bool = False,
) -> bool:
    profile = await repository_async(repository).get_chat_profile(chat_id)
    if profile is None or not bool(profile["pin_enabled"]):
        return False
    if profile.get("pending_pin_message_id") != message_id:
        await repository_async(repository).set_chat_pending_pin(
            chat_id,
            message_id=message_id,
            schedule_date=schedule_date,
            schedule_hash=schedule_hash,
        )
    previous_id = profile.get("last_pinned_message_id")
    try:
        if not permission_checked and not await bot_can_pin(bot, chat_id):
            await repository_async(repository).defer_chat_pin(chat_id, now=now)
            return False
        await bot.pin_chat_message(
            chat_id=chat_id,
            message_id=message_id,
            disable_notification=True,
        )
        if previous_id is not None and int(previous_id) != message_id:
            await _unpin_message(bot, chat_id, int(previous_id))
    except TelegramBadRequest as error:
        if _message_is_absent(error, operation="pin"):
            await repository_async(repository).clear_chat_pending_pin(chat_id)
        await repository_async(repository).defer_chat_pin(chat_id, now=now)
        logger.exception("Could not pin schedule in chat %s", chat_id)
        return False
    except TelegramAPIError:
        await repository_async(repository).defer_chat_pin(chat_id, now=now)
        logger.exception("Could not pin schedule in chat %s", chat_id)
        return False
    await repository_async(repository).set_chat_last_pin(
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
    return current_schedule(repository, role="student", target=target, now=now)


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
    stale_after_minutes: int = 120,
) -> bool | None:
    """Ensure a chat pins the schedule relevant at the given local time."""
    async with _chat_pin_lock(repository, chat_id):
        return await _sync_chat_schedule_pin(
            bot, repository, chat_id=chat_id, target=target, now=now,
            stale_after_minutes=stale_after_minutes,
        )


async def _sync_chat_schedule_pin(
    bot: Bot,
    repository: ScheduleRepository,
    *,
    chat_id: int,
    target: str,
    now: datetime,
    stale_after_minutes: int = 120,
) -> bool | None:
    profile = await repository_async(repository).get_chat_profile(chat_id)
    if profile is None or not bool(profile["pin_enabled"]):
        return False
    target = str(profile["target"])

    selected = await repository_async(repository).call(
        schedule_for_pin, repository, target=target, now=now
    )
    if selected is None:
        return None
    lesson_date, lessons = selected
    state = await repository_async(repository).schedule_freshness()
    notice = freshness_notice(state, now=now, stale_after_minutes=stale_after_minutes, show_success=False)
    photo = await schedule_photo(
        lessons, role="student", target=target, lesson_date=lesson_date, caption=notice or None,
    )
    mode = "photo-v1" if photo is not None else "text"
    schedule_hash = hashlib.sha256((_schedule_hash(target, lessons) + notice + mode).encode()).hexdigest()
    if (
        profile.get("last_pinned_message_id") is not None
        and profile.get("last_pinned_schedule_date") == lesson_date
        and profile.get("last_pinned_schedule_hash") == schedule_hash
        and profile.get("pending_pin_message_id") is None
    ):
        return True

    retry_after = profile.get("pin_retry_after")
    if isinstance(retry_after, datetime) and _utc_now(retry_after) > _utc_now(now):
        return False
    try:
        if not await bot_can_pin(bot, chat_id):
            await repository_async(repository).defer_chat_pin(chat_id, now=now)
            return False
    except TelegramAPIError:
        await repository_async(repository).defer_chat_pin(chat_id, now=now)
        logger.exception("Could not check pin permissions in chat %s", chat_id)
        return False

    message_text = format_schedule(lessons, target, lesson_date, "student")
    if notice:
        message_text += "\n\n" + notice
    pending_id = profile.get("pending_pin_message_id")
    if pending_id is not None:
        if (
            profile.get("pending_pin_schedule_date") != lesson_date
            or profile.get("pending_pin_schedule_hash") != schedule_hash
        ):
            try:
                await _edit_schedule_pin(bot, chat_id, int(pending_id), message_text, photo)
            except TelegramBadRequest as error:
                if _message_is_absent(error, operation="edit"):
                    await repository_async(repository).clear_chat_pending_pin(chat_id)
                    pending_id = None
                elif _cannot_edit(error):
                    try:
                        await _unpin_message(bot, chat_id, int(pending_id))
                    except TelegramAPIError:
                        await repository_async(repository).defer_chat_pin(chat_id, now=now)
                        logger.exception(
                            "Could not retire pending schedule pin in chat %s", chat_id
                        )
                        return False
                    await repository_async(repository).clear_chat_pending_pin(chat_id)
                    pending_id = None
                elif "message is not modified" not in str(error).casefold():
                    await repository_async(repository).defer_chat_pin(chat_id, now=now)
                    logger.exception(
                        "Could not update pending schedule pin in chat %s", chat_id
                    )
                    return False
            except TelegramAPIError:
                await repository_async(repository).defer_chat_pin(chat_id, now=now)
                logger.exception(
                    "Could not update pending schedule pin in chat %s", chat_id
                )
                return False
            if pending_id is not None:
                await repository_async(repository).set_chat_pending_pin(
                    chat_id,
                    message_id=int(pending_id),
                    schedule_date=lesson_date,
                    schedule_hash=schedule_hash,
                )
        if pending_id is not None:
            return await _pin_schedule_message(
                bot,
                repository,
                chat_id=chat_id,
                message_id=int(pending_id),
                schedule_date=lesson_date,
                schedule_hash=schedule_hash,
                now=now,
                permission_checked=True,
            )

    previous_id = profile.get("last_pinned_message_id")
    if previous_id is not None and profile.get("last_pinned_schedule_date") == lesson_date:
        try:
            await _edit_schedule_pin(bot, chat_id, int(previous_id), message_text, photo)
        except TelegramBadRequest as error:
            if "message is not modified" in str(error).casefold():
                await repository_async(repository).set_chat_last_pin(
                    chat_id,
                    message_id=int(previous_id),
                    schedule_date=lesson_date,
                    schedule_hash=schedule_hash,
                )
                return True
            if (
                not _message_is_absent(error, operation="edit")
                and not _cannot_edit(error)
            ):
                await repository_async(repository).defer_chat_pin(chat_id, now=now)
                logger.exception("Could not edit schedule pin in chat %s", chat_id)
                return False
            logger.info("Could not edit schedule pin %s; replacing it", previous_id)
        except TelegramAPIError:
            await repository_async(repository).defer_chat_pin(chat_id, now=now)
            logger.exception("Could not edit schedule pin in chat %s", chat_id)
            return False
        else:
            await repository_async(repository).set_chat_last_pin(
                chat_id,
                message_id=int(previous_id),
                schedule_date=lesson_date,
                schedule_hash=schedule_hash,
            )
            return True

    try:
        if photo is not None:
            sent = await bot.send_photo(chat_id, photo.media, caption=photo.caption, parse_mode=None)
        else:
            sent = await bot.send_message(chat_id, message_text)
    except TelegramAPIError:
        await repository_async(repository).defer_chat_pin(chat_id, now=now)
        logger.exception("Could not publish schedule pin in chat %s", chat_id)
        return False
    await repository_async(repository).set_chat_pending_pin(
        chat_id,
        message_id=sent.message_id,
        schedule_date=lesson_date,
        schedule_hash=schedule_hash,
    )
    return await _pin_schedule_message(
        bot,
        repository,
        chat_id=chat_id,
        message_id=sent.message_id,
        schedule_date=lesson_date,
        schedule_hash=schedule_hash,
        now=now,
        permission_checked=True,
    )


async def sync_schedule_pins(
    bot: Bot,
    repository: ScheduleRepository,
    *,
    now: datetime,
    stale_after_minutes: int = 120,
) -> None:
    """Refresh all enabled group pins without one broken chat stopping others."""
    for profile in await repository_async(repository).pinned_chats():
        chat_id = int(profile["chat_id"])
        try:
            await sync_chat_schedule_pin(
                bot,
                repository,
                chat_id=chat_id,
                target=str(profile["target"]),
                now=now,
                stale_after_minutes=stale_after_minutes,
            )
        except TelegramAPIError:
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
    async with _chat_pin_lock(repository, chat_id):
        profile = await repository_async(repository).get_chat_profile(chat_id)
        if profile is None:
            return True
        message_ids = dict.fromkeys(
            int(message_id)
            for message_id in (
                profile.get("last_pinned_message_id"),
                profile.get("pending_pin_message_id"),
            )
            if message_id is not None
        )
        removed = True
        for message_id in message_ids:
            try:
                await _unpin_message(bot, chat_id, message_id)
            except TelegramAPIError:
                removed = False
                logger.exception("Could not unpin schedule in chat %s", chat_id)
        if not removed:
            return False
        await repository_async(repository).clear_chat_last_pin(chat_id)
        return True
