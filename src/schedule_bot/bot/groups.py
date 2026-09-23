from __future__ import annotations

import html
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from schedule_bot.analytics import UsageAnalytics
from schedule_bot.config import Settings
from schedule_bot.message_utils import answer_in_chunks
from schedule_bot.pinning import pin_schedule_message
from schedule_bot.presentation import format_schedule, format_week
from schedule_bot.repository import ScheduleRepository


router = Router(name="group_chats")
GROUP_CHAT_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}
router.message.filter(F.chat.type.in_(GROUP_CHAT_TYPES))
router.callback_query.filter(F.message.chat.type.in_(GROUP_CHAT_TYPES))


def group_selection_keyboard(groups: list[str]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for index in range(0, len(groups), 3):
        rows.append(
            [
                InlineKeyboardButton(
                    text=group,
                    callback_data=f"group_setup:{group}",
                )
                for group in groups[index : index + 3]
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _is_chat_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    member = await bot.get_chat_member(chat_id, user_id)
    return member.status in {
        ChatMemberStatus.CREATOR,
        ChatMemberStatus.ADMINISTRATOR,
    }


async def _require_admin(message: Message, bot: Bot) -> bool:
    if message.from_user and await _is_chat_admin(
        bot, message.chat.id, message.from_user.id
    ):
        return True
    await message.answer("Эта настройка доступна только администраторам чата.")
    return False


async def _bot_can_pin(bot: Bot, chat_id: int) -> bool:
    member = await bot.get_chat_member(chat_id, bot.id)
    if member.status == ChatMemberStatus.CREATOR:
        return True
    return (
        member.status == ChatMemberStatus.ADMINISTRATOR
        and bool(getattr(member, "can_pin_messages", False))
    )


@router.message(Command("setup"))
async def setup_group(
    message: Message,
    bot: Bot,
    repository: ScheduleRepository,
) -> None:
    if not await _require_admin(message, bot):
        return
    groups = repository.list_groups()
    if not groups:
        await message.answer("Расписание ещё не загружено. Попробуйте позже.")
        return
    await message.answer(
        "Выберите учебную группу для этого чата:",
        reply_markup=group_selection_keyboard(groups),
    )


@router.callback_query(F.data.startswith("group_setup:"))
async def save_group_setup(
    callback: CallbackQuery,
    bot: Bot,
    repository: ScheduleRepository,
    analytics: UsageAnalytics,
) -> None:
    if callback.message is None or not await _is_chat_admin(
        bot, callback.message.chat.id, callback.from_user.id
    ):
        await callback.answer(
            "Настраивать бота может только администратор.", show_alert=True
        )
        return
    target = callback.data.split(":", 1)[1]
    if target not in repository.list_groups():
        await callback.answer("Эта группа больше не найдена.", show_alert=True)
        return
    repository.save_chat_profile(
        callback.message.chat.id,
        chat_type=callback.message.chat.type.value,
        chat_title=callback.message.chat.title,
        target=target,
        configured_by=callback.from_user.id,
    )
    await analytics.track(
        "group_configured",
        actor_id=callback.message.chat.id,
        actor_kind="chat",
        chat_type=callback.message.chat.type.value,
    )
    await callback.message.answer(
        "Готово. Этот чат подключён к группе "
        f"<b>{html.escape(target)}</b>.\n\n"
        "Команды: /today, /tomorrow, /week, /next, /settings"
    )
    await callback.answer()


def _today(settings: Settings) -> date:
    return datetime.now(ZoneInfo(settings.timezone)).date()


async def _send_day(
    message: Message,
    repository: ScheduleRepository,
    analytics: UsageAnalytics,
    *,
    lesson_date: date,
    scope: str,
) -> None:
    profile = repository.get_chat_profile(message.chat.id)
    if profile is None or not bool(profile["active"]):
        await message.answer(
            "Чат ещё не настроен. Администратор должен выполнить /setup."
        )
        return
    target = str(profile["target"])
    lessons = repository.lessons_for(
        role="student", target=target, lesson_date=lesson_date
    )
    sent_messages = await answer_in_chunks(
        message,
        format_schedule(
            lessons,
            target=target,
            lesson_date=lesson_date,
            role="student",
        )
    )
    await analytics.track(
        "schedule_requested",
        actor_id=message.from_user.id if message.from_user else message.chat.id,
        actor_kind="user" if message.from_user else "chat",
        chat_type=message.chat.type.value,
        properties={
            "scope": scope,
            "result": "found" if lessons else "empty",
            "lesson_count": len(lessons),
        },
    )
    if bool(profile["pin_enabled"]) and lessons:
        await pin_schedule_message(
            message.bot,
            repository,
            chat_id=message.chat.id,
            message_id=sent_messages[0].message_id,
        )


@router.message(Command("schedule", "today"))
async def today_schedule(
    message: Message,
    repository: ScheduleRepository,
    settings: Settings,
    analytics: UsageAnalytics,
) -> None:
    await _send_day(
        message,
        repository,
        analytics,
        lesson_date=_today(settings),
        scope="today",
    )


@router.message(Command("tomorrow"))
async def tomorrow_schedule(
    message: Message,
    repository: ScheduleRepository,
    settings: Settings,
    analytics: UsageAnalytics,
) -> None:
    await _send_day(
        message,
        repository,
        analytics,
        lesson_date=_today(settings) + timedelta(days=1),
        scope="tomorrow",
    )


@router.message(Command("next"))
async def next_schedule(
    message: Message,
    repository: ScheduleRepository,
    settings: Settings,
    analytics: UsageAnalytics,
) -> None:
    profile = repository.get_chat_profile(message.chat.id)
    if profile is None:
        await message.answer(
            "Чат ещё не настроен. Администратор должен выполнить /setup."
        )
        return
    target = str(profile["target"])
    start = _today(settings) + timedelta(days=1)
    for lesson_date in repository.available_dates(from_date=start, limit=21):
        if lesson_date < start:
            continue
        lessons = repository.lessons_for(
            role="student", target=target, lesson_date=lesson_date
        )
        if lessons:
            await _send_day(
                message,
                repository,
                analytics,
                lesson_date=lesson_date,
                scope="next",
            )
            return
    await message.answer("Следующий учебный день пока не опубликован.")


@router.message(Command("week"))
async def week_schedule(
    message: Message,
    repository: ScheduleRepository,
    settings: Settings,
    analytics: UsageAnalytics,
) -> None:
    profile = repository.get_chat_profile(message.chat.id)
    if profile is None:
        await message.answer(
            "Чат ещё не настроен. Администратор должен выполнить /setup."
        )
        return
    target = str(profile["target"])
    start = _today(settings)
    end = start + timedelta(days=6)
    dates = [
        value
        for value in repository.available_dates(from_date=start, limit=14)
        if start <= value <= end
    ]
    schedule = {
        lesson_date: repository.lessons_for(
            role="student", target=target, lesson_date=lesson_date
        )
        for lesson_date in dates
    }
    schedule = {key: value for key, value in schedule.items() if value}
    await answer_in_chunks(
        message,
        format_week(schedule, target=target, role="student"),
    )
    await analytics.track(
        "schedule_requested",
        actor_id=message.from_user.id if message.from_user else message.chat.id,
        actor_kind="user" if message.from_user else "chat",
        chat_type=message.chat.type.value,
        properties={
            "scope": "week",
            "result": "found" if schedule else "empty",
            "lesson_count": sum(len(items) for items in schedule.values()),
        },
    )


@router.message(Command("notifications"))
async def toggle_group_notifications(
    message: Message,
    bot: Bot,
    repository: ScheduleRepository,
    analytics: UsageAnalytics,
) -> None:
    if not await _require_admin(message, bot):
        return
    profile = repository.get_chat_profile(message.chat.id)
    if profile is None:
        await message.answer("Сначала настройте чат командой /setup.")
        return
    enabled = not bool(profile["notifications"])
    repository.set_chat_notifications(message.chat.id, enabled)
    await message.answer(
        f"Автоматические уведомления {'включены' if enabled else 'выключены'}."
    )
    await analytics.track(
        "notifications_toggled",
        actor_id=message.chat.id,
        actor_kind="chat",
        chat_type=message.chat.type.value,
        properties={"enabled": enabled},
    )


@router.message(Command("pin"))
async def toggle_group_pin(
    message: Message,
    bot: Bot,
    repository: ScheduleRepository,
    analytics: UsageAnalytics,
) -> None:
    if not await _require_admin(message, bot):
        return
    profile = repository.get_chat_profile(message.chat.id)
    if profile is None:
        await message.answer("Сначала настройте чат командой /setup.")
        return
    enabled = not bool(profile["pin_enabled"])
    if enabled and not await _bot_can_pin(bot, message.chat.id):
        await message.answer(
            "Сначала назначьте бота администратором и разрешите ему "
            "закреплять сообщения."
        )
        return
    repository.set_chat_pin(message.chat.id, enabled)
    suffix = (
        "Бот будет закреплять своё актуальное расписание."
        if enabled
        else "Новые расписания больше не будут закрепляться."
    )
    await message.answer(f"Закрепление {'включено' if enabled else 'выключено'}. {suffix}")
    await analytics.track(
        "pin_toggled",
        actor_id=message.chat.id,
        actor_kind="chat",
        chat_type=message.chat.type.value,
        properties={"enabled": enabled},
    )


@router.message(Command("settings"))
async def group_settings(
    message: Message,
    repository: ScheduleRepository,
) -> None:
    profile = repository.get_chat_profile(message.chat.id)
    if profile is None:
        await message.answer("Чат не настроен. Используйте /setup.")
        return
    await message.answer(
        "<b>Настройки чата</b>\n\n"
        f"Группа: <b>{html.escape(str(profile['target']))}</b>\n"
        f"Уведомления: {'включены' if profile['notifications'] else 'выключены'}\n"
        f"Закрепление: {'включено' if profile['pin_enabled'] else 'выключено'}\n\n"
        "Изменить: /setup, /notifications, /pin"
    )


@router.my_chat_member()
async def bot_membership_changed(
    event: ChatMemberUpdated,
    repository: ScheduleRepository,
) -> None:
    if event.chat.type not in GROUP_CHAT_TYPES:
        return
    if event.new_chat_member.status in {
        ChatMemberStatus.LEFT,
        ChatMemberStatus.KICKED,
    }:
        repository.deactivate_chat(event.chat.id)
