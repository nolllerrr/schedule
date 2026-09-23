from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from schedule_bot.repository import ScheduleRepository


logger = logging.getLogger(__name__)


async def pin_schedule_message(
    bot: Bot,
    repository: ScheduleRepository,
    *,
    chat_id: int,
    message_id: int,
) -> bool:
    """Replace only the schedule pin previously created by this bot."""
    profile = repository.get_chat_profile(chat_id)
    if profile is None or not bool(profile["pin_enabled"]):
        return False
    previous_id = profile.get("last_pinned_message_id")
    try:
        if previous_id and int(previous_id) != message_id:
            try:
                await bot.unpin_chat_message(chat_id, int(previous_id))
            except TelegramBadRequest:
                logger.info("Previous bot pin %s is no longer available", previous_id)
        await bot.pin_chat_message(
            chat_id,
            message_id,
            disable_notification=True,
        )
    except (TelegramBadRequest, TelegramForbiddenError):
        logger.exception("Could not pin schedule in chat %s", chat_id)
        return False
    repository.set_chat_last_message(chat_id, message_id)
    return True
