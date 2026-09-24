from __future__ import annotations

import html
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from schedule_bot.analytics import UsageAnalytics
from schedule_bot.bot.menus import (
    callback_data,
    callback_parts,
    dates_page_from_action,
    dates_menu_keyboard,
    initial_dates_page,
    schedule_menu_keyboard,
    schedule_result_keyboard,
    week_menu_keyboard,
)
from schedule_bot.config import Settings
from schedule_bot.pinning import sync_chat_schedule_pin, unpin_schedule_messages
from schedule_bot.presentation import format_schedule
from schedule_bot.repository import ScheduleRepository


router = Router(name="group_chats")
GROUP_MENU_PREFIX = "gnav"
GROUP_CHAT_TYPES = {"group", "supergroup"}
router.message.filter(F.chat.type.in_(GROUP_CHAT_TYPES))
router.callback_query.filter(F.message.chat.type.in_(GROUP_CHAT_TYPES))


def chat_type_value(value: ChatType | str) -> str:
    """Normalize aiogram versions that expose Chat.type as enum or string."""
    return str(getattr(value, "value", value))


def group_selection_keyboard(
    groups: list[str], owner_id: int
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for index in range(0, len(groups), 3):
        rows.append(
            [
                InlineKeyboardButton(
                    text=group,
                    callback_data=callback_data(
                        "group_setup", owner_id, group
                    ),
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


async def _publish_and_pin_schedule(
    bot: Bot,
    repository: ScheduleRepository,
    settings: Settings,
    *,
    chat_id: int,
    target: str,
) -> bool | None:
    """Publish the schedule that is relevant at the current local time."""
    return await sync_chat_schedule_pin(
        bot,
        repository,
        chat_id=chat_id,
        target=target,
        now=_now(settings),
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
        reply_markup=group_selection_keyboard(groups, message.from_user.id),
    )


@router.callback_query(F.data.startswith("group_setup:"))
async def save_group_setup(
    callback: CallbackQuery,
    bot: Bot,
    repository: ScheduleRepository,
    settings: Settings,
    analytics: UsageAnalytics,
) -> None:
    if callback.message is None or not await _is_chat_admin(
        bot, callback.message.chat.id, callback.from_user.id
    ):
        await callback.answer(
            "Настраивать бота может только администратор.", show_alert=True
        )
        return
    setup_parts = callback_parts(callback.data, "group_setup")
    if setup_parts is None:
        await callback.answer(
            "Это меню настройки устарело. Выполните /setup ещё раз.",
            show_alert=True,
        )
        return
    owner_id, target = setup_parts
    if owner_id != callback.from_user.id:
        await callback.answer(
            "Выбирать группу может только тот, кто открыл это меню.",
            show_alert=True,
        )
        return
    if target not in repository.list_groups():
        await callback.answer("Эта группа больше не найдена.", show_alert=True)
        return

    chat_id = callback.message.chat.id
    previous_profile = repository.get_chat_profile(chat_id)
    previous_target = (
        str(previous_profile["target"]) if previous_profile is not None else None
    )
    target_changed = previous_target is not None and previous_target != target
    tracked_pin = (
        previous_profile is not None
        and previous_profile.get("last_pinned_message_id") is not None
    )
    pin_enabled = bool(previous_profile and previous_profile["pin_enabled"])
    pin_removed = True

    # A pinned schedule belongs to the selected group. Remove it before changing
    # the profile, including stale tracked pins left after pinning was disabled.
    if target_changed and tracked_pin:
        pin_removed = await unpin_schedule_messages(
            bot,
            repository,
            chat_id=chat_id,
        )

    repository.save_chat_profile(
        chat_id,
        chat_type=chat_type_value(callback.message.chat.type),
        chat_title=callback.message.chat.title,
        target=target,
        configured_by=callback.from_user.id,
    )

    pin_result: bool | None = None
    callback_notice = ""
    callback_alert = False
    if target_changed and tracked_pin and not pin_removed:
        callback_notice = (
            "Группа изменена, но старое расписание не удалось открепить. "
            "Проверьте права бота."
        )
        callback_alert = True
    elif target_changed and pin_enabled:
        pin_result = await _publish_and_pin_schedule(
            bot,
            repository,
            settings,
            chat_id=chat_id,
            target=target,
        )
        if pin_result is True:
            callback_notice = "Группа изменена, новое расписание закреплено."
        elif pin_result is None:
            callback_notice = (
                "Группа изменена. Новое расписание будет закреплено после публикации."
            )
        else:
            callback_notice = (
                "Группа изменена, но новое расписание не удалось закрепить. "
                "Проверьте права бота."
            )
            callback_alert = True
    elif target_changed and tracked_pin:
        callback_notice = "Группа изменена, старое расписание откреплено."

    await analytics.track(
        "group_configured",
        actor_id=chat_id,
        actor_kind="chat",
        chat_type=chat_type_value(callback.message.chat.type),
        properties={
            "target_changed": target_changed,
            "pin_enabled": pin_enabled,
            "old_pin_removed": pin_removed,
            "new_pin_result": pin_result,
        },
    )
    await callback.message.edit_text(
        group_menu_text(repository.get_chat_profile(chat_id)),
        reply_markup=schedule_menu_keyboard(
            GROUP_MENU_PREFIX,
            callback.from_user.id,
            include_settings=True,
        ),
    )
    await callback.answer(callback_notice, show_alert=callback_alert)


def group_menu_text(profile: dict[str, object] | None) -> str:
    if profile is None:
        return "Чат ещё не настроен. Администратор должен выполнить /setup."
    return (
        "📚 <b>Расписание группы</b>\n"
        f"Группа: <b>{html.escape(str(profile['target']))}</b>\n\n"
        "Выберите период:"
    )


def group_settings_keyboard(
    owner_id: int,
    profile: dict[str, object],
) -> InlineKeyboardMarkup:
    notifications = "вкл" if profile["notifications"] else "выкл"
    pin = "вкл" if profile["pin_enabled"] else "выкл"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Уведомления: {notifications}",
                    callback_data=callback_data(
                        GROUP_MENU_PREFIX, owner_id, "toggle_notifications"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"Закрепление: {pin}",
                    callback_data=callback_data(
                        GROUP_MENU_PREFIX, owner_id, "toggle_pin"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text="Сменить учебную группу",
                    callback_data=callback_data(
                        GROUP_MENU_PREFIX, owner_id, "change_group"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text="← К расписанию",
                    callback_data=callback_data(
                        GROUP_MENU_PREFIX, owner_id, "root"
                    ),
                )
            ],
        ]
    )


def group_settings_text(profile: dict[str, object]) -> str:
    return (
        "⚙️ <b>Настройки чата</b>\n\n"
        f"Группа: <b>{html.escape(str(profile['target']))}</b>\n"
        f"Уведомления: {'включены' if profile['notifications'] else 'выключены'}\n"
        f"Закрепление: {'включено' if profile['pin_enabled'] else 'выключено'}"
    )


async def _edit_group_message(
    callback: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup,
) -> None:
    if callback.message is None:
        return
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error).casefold():
            raise


@router.message(Command("menu"))
async def open_group_menu(
    message: Message,
    repository: ScheduleRepository,
) -> None:
    if message.from_user is None:
        return
    profile = repository.get_chat_profile(message.chat.id)
    if profile is None or not bool(profile["active"]):
        await message.answer(
            "Чат ещё не настроен. Администратор должен выполнить /setup."
        )
        return
    await message.answer(
        group_menu_text(profile),
        reply_markup=schedule_menu_keyboard(
            GROUP_MENU_PREFIX,
            message.from_user.id,
            include_settings=True,
        ),
    )


@router.callback_query(F.data.startswith(f"{GROUP_MENU_PREFIX}:"))
async def group_menu_action(
    callback: CallbackQuery,
    bot: Bot,
    repository: ScheduleRepository,
    settings: Settings,
    analytics: UsageAnalytics,
) -> None:
    parsed = callback_parts(callback.data, GROUP_MENU_PREFIX)
    if parsed is None or callback.message is None:
        await callback.answer("Кнопка устарела.", show_alert=True)
        return
    owner_id, action = parsed
    if owner_id != callback.from_user.id:
        await callback.answer(
            "Это меню открыл другой пользователь. Отправьте /menu, чтобы открыть своё.",
            show_alert=True,
        )
        return
    profile = repository.get_chat_profile(callback.message.chat.id)
    if profile is None or not bool(profile["active"]):
        await callback.answer("Чат ещё не настроен.", show_alert=True)
        return

    if action == "root":
        await _edit_group_message(
            callback,
            group_menu_text(profile),
            schedule_menu_keyboard(
                GROUP_MENU_PREFIX, owner_id, include_settings=True
            ),
        )
        await callback.answer()
        return

    admin_actions = {
        "settings",
        "toggle_notifications",
        "toggle_pin",
        "change_group",
    }
    if action in admin_actions:
        callback_notice: str | None = None
        callback_alert = False
        if not await _is_chat_admin(bot, callback.message.chat.id, owner_id):
            await callback.answer(
                "Настройки доступны только администраторам чата.",
                show_alert=True,
            )
            return
        if action == "toggle_notifications":
            repository.set_chat_notifications(
                callback.message.chat.id,
                not bool(profile["notifications"]),
            )
            profile = repository.get_chat_profile(callback.message.chat.id)
            await analytics.track(
                "notifications_toggled",
                actor_id=callback.message.chat.id,
                actor_kind="chat",
                chat_type=chat_type_value(callback.message.chat.type),
                properties={"enabled": bool(profile["notifications"])},
            )
        elif action == "toggle_pin":
            enabled = not bool(profile["pin_enabled"])
            if enabled and not await _bot_can_pin(bot, callback.message.chat.id):
                await callback.answer(
                    "Сначала выдайте боту право закреплять сообщения.",
                    show_alert=True,
                )
                return
            pins_removed = True
            if not enabled:
                pins_removed = await unpin_schedule_messages(
                    bot,
                    repository,
                    chat_id=callback.message.chat.id,
                )
                if not pins_removed:
                    callback_notice = (
                        "Закрепление выключено, но бот не смог открепить своё "
                        "сообщение. Проверьте его права администратора."
                    )
                    callback_alert = True
            repository.set_chat_pin(callback.message.chat.id, enabled)
            profile = repository.get_chat_profile(callback.message.chat.id)
            if enabled:
                pin_result = await _publish_and_pin_schedule(
                    bot,
                    repository,
                    settings,
                    chat_id=callback.message.chat.id,
                    target=str(profile["target"]),
                )
                if pin_result is True:
                    callback_notice = "Актуальное расписание опубликовано и закреплено."
                elif pin_result is None:
                    callback_notice = (
                        "Закрепление включено. Подходящего расписания пока нет — "
                        "бот закрепит его после публикации."
                    )
                else:
                    callback_notice = (
                        "Закрепление включено, но сообщение не удалось закрепить. "
                        "Проверьте права бота."
                    )
                    callback_alert = True
            elif pins_removed:
                callback_notice = "Закрепление выключено, сообщение откреплено."
            await analytics.track(
                "pin_toggled",
                actor_id=callback.message.chat.id,
                actor_kind="chat",
                chat_type=chat_type_value(callback.message.chat.type),
                properties={
                    "enabled": enabled,
                    "pins_removed": pins_removed,
                },
            )
        elif action == "change_group":
            groups = repository.list_groups()
            await _edit_group_message(
                callback,
                "Выберите учебную группу для этого чата:",
                group_selection_keyboard(groups, owner_id),
            )
            await callback.answer()
            return

        await _edit_group_message(
            callback,
            group_settings_text(profile),
            group_settings_keyboard(owner_id, profile),
        )
        await callback.answer(
            callback_notice or "",
            show_alert=callback_alert,
        )
        return

    today_value = _today(settings)
    target = str(profile["target"])
    dates_page = dates_page_from_action(action)
    if dates_page is not None:
        dates = repository.published_dates()
        if not dates:
            await callback.answer("В базе пока нет расписания.", show_alert=True)
            return
        if action == "dates":
            dates_page = initial_dates_page(dates, today_value)
        await _edit_group_message(
            callback,
            "📅 <b>Выберите дату</b>",
            dates_menu_keyboard(GROUP_MENU_PREFIX, owner_id, dates, page=dates_page),
        )
        await callback.answer()
        return

    if action == "week":
        end = today_value + timedelta(days=6)
        published_dates = [
            value
            for value in repository.available_dates(from_date=today_value, limit=14)
            if today_value <= value <= end
        ]
        dates = [
            lesson_date
            for lesson_date in published_dates
            if repository.lessons_for(
                role="student", target=target, lesson_date=lesson_date
            )
        ]
        text = (
            "📅 <b>Расписание на неделю</b>\n"
            f"Группа: <b>{html.escape(target)}</b>\n\n"
        )
        if not dates:
            text += "На ближайшие семь дней занятий нет."
            keyboard = schedule_result_keyboard(
                GROUP_MENU_PREFIX,
                owner_id,
                include_settings=True,
            )
        else:
            text += "Выберите день:"
            keyboard = week_menu_keyboard(GROUP_MENU_PREFIX, owner_id, dates)
        await _edit_group_message(
            callback,
            text,
            keyboard,
        )
        await callback.answer()
        return

    lesson_date: date | None = None
    scope = action
    from_week = False
    if action == "today":
        lesson_date = today_value
    elif action == "tomorrow":
        lesson_date = today_value + timedelta(days=1)
    elif action.startswith("date_"):
        try:
            lesson_date = date.fromisoformat(action.removeprefix("date_"))
            scope = "date"
        except ValueError:
            pass
    elif action.startswith("weekdate_"):
        try:
            lesson_date = date.fromisoformat(action.removeprefix("weekdate_"))
            scope = "week"
            from_week = True
        except ValueError:
            pass

    if lesson_date is None:
        await callback.answer("Кнопка устарела.", show_alert=True)
        return

    lessons = repository.lessons_for(
        role="student", target=target, lesson_date=lesson_date
    )
    await _edit_group_message(
        callback,
        format_schedule(lessons, target, lesson_date, "student"),
        schedule_result_keyboard(
            GROUP_MENU_PREFIX,
            owner_id,
            include_settings=True,
            back_action="week" if from_week else "root",
            back_text="← К дням недели" if from_week else "Меню расписания",
        ),
    )
    await analytics.track(
        "schedule_requested",
        actor_id=owner_id,
        actor_kind="user",
        chat_type=chat_type_value(callback.message.chat.type),
        properties={
            "scope": scope,
            "result": "found" if lessons else "empty",
            "lesson_count": len(lessons),
        },
    )
    await callback.answer()


def _today(settings: Settings) -> date:
    return _now(settings).date()


def _now(settings: Settings) -> datetime:
    return datetime.now(ZoneInfo(settings.timezone))


@router.message(Command("help"))
async def group_help(message: Message) -> None:
    await message.answer(
        "ℹ️ <b>Помощь</b>\n\n"
        "/menu — открыть личное меню расписания в этом чате\n"
        "/setup — выбрать учебную группу, только для администраторов\n"
        "/help — показать эту справку\n\n"
        "Кнопками меню может пользоваться только открывший его участник. "
        "Настройки уведомлений, закрепления и группы доступны администраторам "
        "через кнопку «Настройки чата»."
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
