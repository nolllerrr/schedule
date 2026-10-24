from __future__ import annotations

import asyncio
import html
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import date, datetime, timedelta
from typing import Literal, cast
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramNetworkError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from schedule_bot.analytics import UsageAnalytics, format_stats
from schedule_bot.bot.cards import card_action, send_schedule_card
from schedule_bot.card_service import close_card_service
from schedule_bot.bot.groups import router as group_router
from schedule_bot.bot.menus import (
    callback_parts,
    dates_page_from_action,
    dates_menu_keyboard,
    initial_dates_page,
    schedule_menu_keyboard,
    schedule_result_keyboard,
    week_menu_keyboard,
)
from schedule_bot.bot.session import create_telegram_session
from schedule_bot.bot.storage import ExpiringMemoryStorage
from schedule_bot.bot.signals import ScheduleSignalListener, create_signal_router
from schedule_bot.config import Settings
from schedule_bot.domain import ImportResult
from schedule_bot.diagnostics import safe_update_diagnostic
from schedule_bot.downloader import ScheduleDownloadError, ScheduleDownloader
from schedule_bot.message_utils import answer_in_chunks
from schedule_bot.maintenance import backup_before_migration, create_database_backup, cleanup_technical_data
from schedule_bot.notifier import ScheduleNotifier
from schedule_bot.outage import ScheduleOutageTracker
from schedule_bot.parser import ExcelScheduleParser
from schedule_bot.pinning import sync_schedule_pins
from schedule_bot.presentation import format_schedule, format_week
from schedule_bot.repository import ScheduleRepository
from schedule_bot.reactions import GrokReactionWorker
from schedule_bot.async_repository import repository_async
from schedule_bot.freshness import with_freshness
from schedule_bot.services import ScheduleUpdater
from schedule_bot.schedule_selection import current_schedule


logger = logging.getLogger(__name__)
PRIVATE_MENU_PREFIX = "pnav"
CURRENT_SCHEDULE_BUTTON = "Щас"
router = Router(name="private_chats")
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)


class ProfileSetup(StatesGroup):
    waiting_teacher_query = State()
    choosing_teacher = State()


async def configure_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Выбрать профиль"),
            BotCommand(command="menu", description="Открыть расписание"),
            BotCommand(command="help", description="Помощь по командам"),
            BotCommand(command="privacy", description="Какие данные хранит бот"),
            BotCommand(command="delete_me", description="Удалить мои данные"),
        ],
        scope=BotCommandScopeAllPrivateChats(),
    )
    await bot.set_my_commands(
        [
            BotCommand(command="menu", description="Открыть расписание"),
            BotCommand(command="setup", description="Выбрать учебную группу"),
            BotCommand(command="help", description="Помощь по командам"),
        ],
        scope=BotCommandScopeAllGroupChats(),
    )


def role_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Студент", callback_data="role:student"
                ),
                InlineKeyboardButton(
                    text="Преподаватель", callback_data="role:teacher"
                ),
            ]
        ]
    )


def groups_keyboard(groups: list[str]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for index in range(0, len(groups), 3):
        rows.append(
            [
                InlineKeyboardButton(text=group, callback_data=f"group:{group}")
                for group in groups[index : index + 3]
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def teachers_keyboard(teachers: list[str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=name, callback_data=f"teacher:{index}")]
            for index, name in enumerate(teachers)
        ]
    )


def main_keyboard(notifications: bool = True) -> ReplyKeyboardMarkup:
    state = "включены" if notifications else "выключены"
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=CURRENT_SCHEDULE_BUTTON)],
            [
                KeyboardButton(text=f"Уведомления: {state}"),
                KeyboardButton(text="Сменить профиль"),
            ],
        ],
        resize_keyboard=True,
    )


@router.message(CommandStart())
async def start(
    message: Message,
    state: FSMContext,
    analytics: UsageAnalytics,
) -> None:
    await state.clear()
    if message.from_user:
        await analytics.track(
            "bot_started",
            actor_id=message.from_user.id,
            chat_type="private",
        )
    await message.answer(
        "Кому показать расписание?", reply_markup=role_keyboard()
    )


@router.callback_query(F.data == "role:student")
async def choose_student_group(
    callback: CallbackQuery, repository: ScheduleRepository, state: FSMContext
) -> None:
    await state.clear()
    groups = await repository_async(repository).list_groups()
    if not groups:
        await callback.message.answer(
            "Расписание ещё не загружено. Попробуйте позже."
        )
    else:
        await callback.message.answer(
            "Выберите группу:", reply_markup=groups_keyboard(groups)
        )
    await callback.answer()


@router.callback_query(F.data == "role:teacher")
async def request_teacher(
    callback: CallbackQuery, state: FSMContext
) -> None:
    await state.set_state(ProfileSetup.waiting_teacher_query)
    await callback.message.answer("Введите фамилию преподавателя:")
    await callback.answer()


@router.callback_query(F.data.startswith("group:"))
async def save_student_group(
    callback: CallbackQuery,
    repository: ScheduleRepository,
    analytics: UsageAnalytics,
) -> None:
    group = callback.data.split(":", 1)[1]
    await repository_async(repository).save_profile(
        callback.from_user.id, role="student", target=group
    )
    await analytics.track(
        "profile_configured",
        actor_id=callback.from_user.id,
        chat_type="private",
        properties={"role": "student"},
    )
    await callback.message.answer(
        "Актуальное расписание и настройки доступны на нижней клавиатуре.",
        reply_markup=main_keyboard(),
    )
    await callback.message.edit_text(
        private_menu_text(await repository_async(repository).get_profile(callback.from_user.id)),
        reply_markup=schedule_menu_keyboard(
            PRIVATE_MENU_PREFIX, callback.from_user.id
        ),
    )
    await callback.answer()


@router.message(ProfileSetup.waiting_teacher_query, F.text)
async def find_teacher(
    message: Message, repository: ScheduleRepository, state: FSMContext
) -> None:
    teachers = await repository_async(repository).search_teachers(message.text or "")
    if not teachers:
        await message.answer(
            "Преподаватель не найден. Проверьте фамилию и попробуйте ещё раз."
        )
        return
    await state.update_data(teacher_matches=teachers)
    await state.set_state(ProfileSetup.choosing_teacher)
    await message.answer(
        "Выберите преподавателя:", reply_markup=teachers_keyboard(teachers)
    )


@router.callback_query(ProfileSetup.choosing_teacher, F.data.startswith("teacher:"))
async def save_teacher(
    callback: CallbackQuery,
    repository: ScheduleRepository,
    state: FSMContext,
    analytics: UsageAnalytics,
) -> None:
    data = await state.get_data()
    matches = cast(list[str], data.get("teacher_matches", []))
    try:
        teacher = matches[int(callback.data.split(":", 1)[1])]
    except (IndexError, ValueError):
        await callback.answer("Список устарел. Запустите выбор ещё раз.", show_alert=True)
        return
    await repository_async(repository).save_profile(
        callback.from_user.id, role="teacher", target=teacher
    )
    await analytics.track(
        "profile_configured",
        actor_id=callback.from_user.id,
        chat_type="private",
        properties={"role": "teacher"},
    )
    await state.clear()
    await callback.message.answer(
        "Актуальное расписание и настройки доступны на нижней клавиатуре.",
        reply_markup=main_keyboard(),
    )
    await callback.message.edit_text(
        private_menu_text(await repository_async(repository).get_profile(callback.from_user.id)),
        reply_markup=schedule_menu_keyboard(
            PRIVATE_MENU_PREFIX, callback.from_user.id
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("teacher:"))
async def expired_teacher_choice(callback: CallbackQuery) -> None:
    await callback.answer("Список устарел. Запустите выбор ещё раз.", show_alert=True)


def private_menu_text(profile: dict[str, object] | None) -> str:
    if profile is None:
        return "Сначала выберите профиль командой /start."
    label = "Группа" if profile["role"] == "student" else "Преподаватель"
    return (
        "📚 <b>Расписание</b>\n"
        f"{label}: <b>{html.escape(str(profile['target']))}</b>\n\n"
        "Выберите период:"
    )


async def _edit_private_message(
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
async def open_private_menu(
    message: Message,
    repository: ScheduleRepository,
    settings: Settings | None = None,
) -> None:
    if message.from_user is None:
        return
    profile = await repository_async(repository).get_profile(message.from_user.id)
    if profile is None:
        await message.answer("Сначала выберите профиль командой /start.")
        return
    await message.answer(
        await with_freshness(private_menu_text(profile), repository, settings),
        reply_markup=schedule_menu_keyboard(
            PRIVATE_MENU_PREFIX, message.from_user.id
        ),
    )


@router.callback_query(F.data.startswith(f"{PRIVATE_MENU_PREFIX}:"))
async def private_menu_action(
    callback: CallbackQuery,
    repository: ScheduleRepository,
    settings: Settings,
    analytics: UsageAnalytics,
) -> None:
    parsed = callback_parts(callback.data, PRIVATE_MENU_PREFIX)
    if parsed is None:
        await callback.answer("Кнопка устарела.", show_alert=True)
        return
    owner_id, action = parsed
    if owner_id != callback.from_user.id:
        await callback.answer("Это меню открыто другим пользователем.", show_alert=True)
        return
    profile = await repository_async(repository).get_profile(owner_id)
    if profile is None:
        await callback.answer("Сначала выберите профиль через /start.", show_alert=True)
        return

    if action == "root":
        await _edit_private_message(
            callback,
            await with_freshness(private_menu_text(profile), repository, settings),
            schedule_menu_keyboard(PRIVATE_MENU_PREFIX, owner_id),
        )
        await callback.answer()
        return

    today_value = datetime.now(ZoneInfo(settings.timezone)).date()
    role = cast(Literal["student", "teacher"], profile["role"])
    target = str(profile["target"])
    if action.startswith("card_"):
        await send_schedule_card(
            callback, repository, settings, role=role, target=target, action=action,
        )
        return
    dates_page = dates_page_from_action(action)
    if dates_page is not None:
        dates = await repository_async(repository).published_dates()
        if not dates:
            await callback.answer("В базе пока нет расписания.", show_alert=True)
            return
        if action == "dates":
            dates_page = initial_dates_page(dates, today_value)
        await _edit_private_message(
            callback,
            "📅 <b>Выберите дату</b>",
            dates_menu_keyboard(PRIVATE_MENU_PREFIX, owner_id, dates, page=dates_page),
        )
        await callback.answer()
        return

    if action == "week":
        end = today_value + timedelta(days=6)
        published_dates = [
            value
            for value in await repository_async(repository).available_dates(from_date=today_value, limit=14)
            if today_value <= value <= end
        ]
        dates = [
            lesson_date
            for lesson_date in published_dates
            if await repository_async(repository).lessons_for(
                role=role, target=target, lesson_date=lesson_date
            )
        ]
        label = "Группа" if role == "student" else "Преподаватель"
        text = (
            "📅 <b>Расписание на неделю</b>\n"
            f"{label}: <b>{html.escape(target)}</b>\n\n"
        )
        if not dates:
            text += "На ближайшие семь дней занятий нет."
            keyboard = schedule_result_keyboard(PRIVATE_MENU_PREFIX, owner_id)
        else:
            text += "Выберите день:"
            keyboard = week_menu_keyboard(PRIVATE_MENU_PREFIX, owner_id, dates)
        await _edit_private_message(
            callback,
            await with_freshness(text, repository, settings),
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

    lessons = await repository_async(repository).lessons_for(
        role=role,
        target=target,
        lesson_date=lesson_date,
    )
    await _edit_private_message(
        callback,
        await with_freshness(format_schedule(lessons, target, lesson_date, role), repository, settings),
        schedule_result_keyboard(
            PRIVATE_MENU_PREFIX,
            owner_id,
            back_action="week" if from_week else "root",
            back_text="← К дням недели" if from_week else "Меню расписания",
            card_action=card_action(role, lesson_date),
        ),
    )
    await analytics.track(
        "schedule_requested",
        actor_id=owner_id,
        chat_type="private",
        properties={
            "scope": scope,
            "result": "found" if lessons else "empty",
            "lesson_count": len(lessons),
            "role": role,
        },
    )
    await callback.answer()


async def _send_schedule(
    message: Message,
    repository: ScheduleRepository,
    settings: Settings,
    analytics: UsageAnalytics,
    *,
    days: int = 0,
    selected_date: date | None = None,
    current: bool = False,
    scope: str,
) -> None:
    if message.from_user is None:
        return
    profile = await repository_async(repository).get_profile(message.from_user.id)
    if profile is None:
        await message.answer("Сначала выберите профиль командой /start.")
        return
    now = datetime.now(ZoneInfo(settings.timezone))
    role = cast(Literal["student", "teacher"], profile["role"])
    target = str(profile["target"])
    lesson_date: date | None = None
    if current:
        selected = await repository_async(repository).call(
            current_schedule, repository, role=role, target=target, now=now
        )
        if selected is None:
            lessons = []
            text = "Ближайшее расписание с занятиями пока не опубликовано."
        else:
            lesson_date, lessons = selected
            text = format_schedule(lessons, target, lesson_date, role)
    else:
        lesson_date = selected_date or (now.date() + timedelta(days=days))
        lessons = await repository_async(repository).lessons_for(
            role=role, target=target, lesson_date=lesson_date
        )
        text = format_schedule(lessons, target, lesson_date, role)
    await answer_in_chunks(
        message,
        await with_freshness(text, repository, settings),
        reply_markup=main_keyboard(bool(profile["notifications"])),
    )
    if lesson_date is not None and role == "student":
        await _offer_private_card(message, message.from_user.id, lesson_date)
    if message.from_user:
        await analytics.track(
            "schedule_requested",
            actor_id=message.from_user.id,
            chat_type="private",
            properties={
                "scope": scope,
                "result": "found" if lessons else "empty",
                "lesson_count": len(lessons),
                "role": role,
            },
        )


async def _answer_private_control(
    message: Message, text: str, reply_markup: InlineKeyboardMarkup,
) -> None:
    try:
        await message.answer(text, reply_markup=reply_markup)
    except TelegramAPIError as error:
        # The text was already delivered. Optional image controls must not
        # interrupt the existing command or disclose a Telegram error payload.
        logger.warning("Card control could not be sent (%s)", type(error).__name__)


async def _offer_private_card(message: Message, owner_id: int, lesson_date: date) -> None:
    # Telegram accepts either a reply keyboard or inline buttons per message.
    # Keep the existing text/reply keyboard and add a separate inline control.
    await _answer_private_control(
        message,
        f"Расписание на {lesson_date:%d.%m.%Y} картинкой:",
        schedule_result_keyboard(
            PRIVATE_MENU_PREFIX, owner_id,
            card_action=card_action("student", lesson_date),
        ),
    )


@router.message(F.text == "Сегодня")
async def today(
    message: Message,
    repository: ScheduleRepository,
    settings: Settings,
    analytics: UsageAnalytics,
) -> None:
    await _send_schedule(
        message, repository, settings, analytics, days=0, scope="today"
    )


@router.message(F.text.in_({CURRENT_SCHEDULE_BUTTON, "Актуальное расписание"}))
async def show_current_schedule(
    message: Message,
    repository: ScheduleRepository,
    settings: Settings,
    analytics: UsageAnalytics,
) -> None:
    await _send_schedule(
        message, repository, settings, analytics, current=True, scope="current"
    )


@router.message(F.text == "Завтра")
async def tomorrow(
    message: Message,
    repository: ScheduleRepository,
    settings: Settings,
    analytics: UsageAnalytics,
) -> None:
    await _send_schedule(
        message, repository, settings, analytics, days=1, scope="tomorrow"
    )


@router.message(F.text == "Следующий день")
async def next_study_day(
    message: Message,
    repository: ScheduleRepository,
    settings: Settings,
    analytics: UsageAnalytics,
) -> None:
    if message.from_user is None:
        return
    profile = await repository_async(repository).get_profile(message.from_user.id)
    if profile is None:
        await message.answer("Сначала выберите профиль командой /start.")
        return
    role = cast(Literal["student", "teacher"], profile["role"])
    target = str(profile["target"])
    start = datetime.now(ZoneInfo(settings.timezone)).date() + timedelta(days=1)
    for lesson_date in await repository_async(repository).available_dates(from_date=start, limit=21):
        if lesson_date < start:
            continue
        lessons = await repository_async(repository).lessons_for(
            role=role, target=target, lesson_date=lesson_date
        )
        if lessons:
            await _send_schedule(
                message,
                repository,
                settings,
                analytics,
                selected_date=lesson_date,
                scope="next",
            )
            return
    await message.answer(await with_freshness("Следующий учебный день пока не опубликован.", repository, settings))
    await analytics.track(
        "schedule_requested",
        actor_id=message.from_user.id,
        chat_type="private",
        properties={"scope": "next", "result": "empty", "lesson_count": 0},
    )


@router.message(F.text == "Неделя")
async def week(
    message: Message,
    repository: ScheduleRepository,
    settings: Settings,
    analytics: UsageAnalytics,
) -> None:
    if message.from_user is None:
        return
    profile = await repository_async(repository).get_profile(message.from_user.id)
    if profile is None:
        await message.answer("Сначала выберите профиль командой /start.")
        return
    role = cast(Literal["student", "teacher"], profile["role"])
    target = str(profile["target"])
    start = datetime.now(ZoneInfo(settings.timezone)).date()
    end = start + timedelta(days=6)
    dates = [
        value
        for value in await repository_async(repository).available_dates(from_date=start, limit=14)
        if start <= value <= end
    ]
    schedule = {
        lesson_date: await repository_async(repository).lessons_for(
            role=role, target=target, lesson_date=lesson_date
        )
        for lesson_date in dates
    }
    schedule = {key: value for key, value in schedule.items() if value}
    await answer_in_chunks(
        message,
        await with_freshness(format_week(schedule, target=target, role=role), repository, settings),
        reply_markup=main_keyboard(bool(profile["notifications"])),
    )
    if role == "student" and schedule:
        await _answer_private_control(
            message,
            "Карточка расписания: выберите день.",
            week_menu_keyboard(PRIVATE_MENU_PREFIX, message.from_user.id, sorted(schedule)),
        )
    await analytics.track(
        "schedule_requested",
        actor_id=message.from_user.id,
        chat_type="private",
        properties={
            "scope": "week",
            "result": "found" if schedule else "empty",
            "lesson_count": sum(len(items) for items in schedule.values()),
            "role": role,
        },
    )


@router.message(F.text == "Выбрать дату")
async def choose_date(
    message: Message, repository: ScheduleRepository, settings: Settings
) -> None:
    dates = await repository_async(repository).published_dates()
    if not dates:
        await message.answer("В базе пока нет расписания.")
        return
    if message.from_user is None:
        return
    await message.answer(
        "📅 <b>Выберите дату</b>",
        reply_markup=dates_menu_keyboard(
            PRIVATE_MENU_PREFIX,
            message.from_user.id,
            dates,
            page=initial_dates_page(
                dates, datetime.now(ZoneInfo(settings.timezone)).date()
            ),
        ),
    )


@router.callback_query(F.data.startswith("date:"))
async def selected_date(
    callback: CallbackQuery,
    repository: ScheduleRepository,
    settings: Settings,
    analytics: UsageAnalytics,
) -> None:
    try:
        lesson_date = date.fromisoformat(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректная дата.", show_alert=True)
        return
    profile = await repository_async(repository).get_profile(callback.from_user.id)
    if profile is None:
        await callback.message.answer("Сначала выберите профиль командой /start.")
    else:
        role = cast(Literal["student", "teacher"], profile["role"])
        target = str(profile["target"])
        lessons = await repository_async(repository).lessons_for(
            role=role, target=target, lesson_date=lesson_date
        )
        await answer_in_chunks(
            callback.message,
            format_schedule(
                lessons,
                target=target,
                lesson_date=lesson_date,
                role=role,
            ),
            reply_markup=main_keyboard(bool(profile["notifications"])),
        )
        if role == "student":
            await _offer_private_card(callback.message, callback.from_user.id, lesson_date)
        await analytics.track(
            "schedule_requested",
            actor_id=callback.from_user.id,
            chat_type="private",
            properties={
                "scope": "date",
                "result": "found" if lessons else "empty",
                "lesson_count": len(lessons),
                "role": role,
            },
        )
    await callback.answer()


@router.message(F.text.startswith("Уведомления:"))
async def toggle_notifications(
    message: Message,
    repository: ScheduleRepository,
    analytics: UsageAnalytics,
) -> None:
    profile = await repository_async(repository).get_profile(message.from_user.id)
    if profile is None:
        await message.answer("Сначала выберите профиль командой /start.")
        return
    enabled = not bool(profile["notifications"])
    await repository_async(repository).set_notifications(message.from_user.id, enabled)
    answer = "включены" if enabled else "выключены"
    await message.answer(
        f"Уведомления {answer}.", reply_markup=main_keyboard(enabled)
    )
    await analytics.track(
        "notifications_toggled",
        actor_id=message.from_user.id,
        chat_type="private",
        properties={"enabled": enabled},
    )


@router.message(F.text == "Сменить профиль")
async def change_profile(
    message: Message,
    state: FSMContext,
    analytics: UsageAnalytics,
) -> None:
    await start(message, state, analytics)


@router.message(Command("privacy"))
async def privacy(message: Message, settings: Settings) -> None:
    await message.answer(
        "<b>Какие данные хранит бот</b>\n\n"
        "Для работы: Telegram ID, выбранную группу или преподавателя и настройку "
        "уведомлений. Для группового чата: его ID, название, настройки и ID "
        "администратора, выполнившего настройку. Для улучшения сервиса: "
        "обезличенные события использования, "
        "тип запрошенного расписания, наличие результата и технические ошибки.\n\n"
        "Бот не сохраняет тексты сообщений, username, имя, IP-адрес, устройство "
        "или список участников групп. Подробные события удаляются через "
        f"{settings.analytics_retention_days} дней."
    )


@router.message(Command("help"))
async def private_help(message: Message, settings: Settings) -> None:
    lines = [
        "ℹ️ <b>Помощь</b>",
        "",
        "/start — выбрать группу или преподавателя",
        "/menu — открыть меню расписания",
        "/help — показать эту справку",
        "/privacy — узнать, какие данные хранит бот",
        "/delete_me — удалить личные данные",
    ]
    if message.from_user and message.from_user.id in settings.admin_telegram_ids:
        lines.extend(
            [
                "",
                "<b>Команды администратора</b>",
                "/update — проверить и импортировать расписание",
                "/stats — статистика за 7 дней",
                "/stats 30 — статистика за выбранное число дней",
            ]
        )
    lines.extend(
        [
            "",
            f"На нижней клавиатуре кнопка «{CURRENT_SCHEDULE_BUTTON}» показывает "
            "сегодняшние занятия до окончания последней пары, затем — ближайший "
            "опубликованный день с занятиями. Там же доступны уведомления и смена профиля. "
            "Другие даты можно выбрать через /menu.",
        ]
    )
    await message.answer("\n".join(lines))


@router.message(Command("delete_me"))
async def request_data_deletion(message: Message) -> None:
    await message.answer(
        "Удалить профиль, настройки и очередь личных уведомлений, связанную обезличенную "
        "статистику? История опубликованных расписаний останется общей.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Удалить мои данные",
                        callback_data="delete_me:confirm",
                    ),
                    InlineKeyboardButton(
                        text="Отмена",
                        callback_data="delete_me:cancel",
                    ),
                ]
            ]
        ),
    )


@router.callback_query(F.data == "delete_me:cancel")
async def cancel_data_deletion(callback: CallbackQuery) -> None:
    await callback.answer("Удаление отменено.")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data == "delete_me:confirm")
async def confirm_data_deletion(
    callback: CallbackQuery,
    repository: ScheduleRepository,
    analytics: UsageAnalytics,
    state: FSMContext,
    notifier: ScheduleNotifier,
) -> None:
    actor_key = analytics.actor_keys("user", callback.from_user.id)
    deleted = await notifier.delete_user_data(callback.from_user.id, actor_key)
    await state.clear()
    if callback.message:
        await callback.message.answer(
            "Ваши данные удалены." if deleted else "Сохранённых данных не найдено.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer()


@router.message(Command("stats"))
async def stats(
    message: Message,
    settings: Settings,
    analytics: UsageAnalytics,
) -> None:
    if message.from_user is None or (
        message.from_user.id not in settings.admin_telegram_ids
    ):
        await message.answer("Команда доступна только администратору.")
        return
    parts = (message.text or "").split(maxsplit=1)
    try:
        days = min(90, max(1, int(parts[1]))) if len(parts) > 1 else 7
    except ValueError:
        await message.answer("Использование: /stats или /stats 30")
        return
    await message.answer(format_stats(await analytics.summary(days=days), days=days))


@router.message(Command("update"))
async def manual_update(
    message: Message,
    bot: Bot,
    settings: Settings,
    repository: ScheduleRepository,
    updater: ScheduleUpdater,
    notifier: ScheduleNotifier,
    update_lock: asyncio.Lock,
    analytics: UsageAnalytics,
    pin_sync_lock: asyncio.Lock | None = None,
    queue_grok_reactions: Callable[[ImportResult], None] | None = None,
) -> None:
    if message.from_user is None or (
        message.from_user.id not in settings.admin_telegram_ids
    ):
        await message.answer("Команда доступна только администратору.")
        return
    stage = "update"
    try:
        async with update_lock:
            result = await updater.update_once()
            stage = "delivery"
            await notifier.notify_import(result)
            stage = "pin"
            async with pin_sync_lock or asyncio.Lock():
                await sync_schedule_pins(
                    bot,
                    repository,
                    now=datetime.now(ZoneInfo(settings.timezone)),
                    stale_after_minutes=settings.schedule_stale_after_minutes,
                )
    except Exception as error:
        logger.exception("Manual schedule update failed")
        await _track_import(analytics, error=error)
        await message.answer(safe_update_diagnostic(error, stage=stage))
        return
    await _track_import(analytics, result=result)
    if queue_grok_reactions is not None:
        queue_grok_reactions(result)
    if result.skipped:
        await message.answer("Новых файлов нет.")
    else:
        await message.answer(
            f"Импорт завершён: {result.lesson_count} занятий, "
            f"изменений — {len(result.changes)}."
        )


async def _track_import(
    analytics: UsageAnalytics,
    *,
    result: ImportResult | None = None,
    error: Exception | None = None,
) -> None:
    try:
        if error is None and result is None:
            raise ValueError("Import statistics require a result or an error")
        await analytics.track(
            "import_failed" if error is not None else "import_completed",
            actor_id="schedule-updater",
            actor_kind="system",
            chat_type="private",
            properties=(
                {"error_type": type(error).__name__}
                if error is not None
                else {
                    "skipped": result.skipped,
                    "lesson_count": result.lesson_count,
                    "change_count": len(result.changes),
                }
            ),
        )
    except Exception:
        # Statistics must never prevent a critical alert or a manual reply.
        logger.exception("Could not record schedule update statistics")


async def automatic_update_job(
    updater: ScheduleUpdater,
    notifier: ScheduleNotifier,
    analytics: UsageAnalytics,
    update_lock: asyncio.Lock,
    pin_job: Callable[[], Awaitable[None]],
    *,
    wait_for_lock: bool = False,
    queue_grok_reactions: Callable[[ImportResult], None] | None = None,
) -> None:
    if update_lock.locked() and not wait_for_lock:
        logger.info("Schedule update already running; automatic check skipped")
        return
    try:
        async with update_lock:
            try:
                result = await updater.update_once(automatic=True)
            except ScheduleDownloadError as error:
                if not error.transient or updater.outage_tracker is None:
                    raise
                # The updater has durably recorded this single automatic check.
                logger.warning("Schedule source check failed: %s", error, exc_info=True)
                await _track_import(analytics, error=error)
                return
            await notifier.notify_import(result)
        await pin_job()
        await _track_import(analytics, result=result)
        if queue_grok_reactions is not None:
            queue_grok_reactions(result)
    except Exception as error:
        logger.exception("Automatic schedule update failed")
        await _track_import(analytics, error=error)
        await notifier.notify_admins_error(error)


async def run_bot(settings: Settings) -> None:
    from schedule_bot.runtime_health import run_runtime_health

    # Never reuse a heartbeat from an abruptly stopped previous run.
    await asyncio.to_thread((settings.database_path.parent / "runtime-health.json").unlink, missing_ok=True)
    repository = ScheduleRepository(settings.database_path)
    migration_backup = await asyncio.to_thread(
        backup_before_migration, repository,
        settings.backups_path,
    )
    if migration_backup:
        logger.info("Pre-migration database backup created: %s", migration_backup)
    await repository_async(repository).initialize()
    downloader = ScheduleDownloader(
        settings.schedule_page_url,
        settings.downloads_path,
        lookback_days=settings.publication_lookback_days,
        lookahead_days=settings.publication_lookahead_days,
        max_publications=settings.publication_max_files,
        building=1,
        proxy_url=settings.schedule_proxy_url,
        max_attempts=settings.schedule_http_max_attempts,
        retry_max_delay=settings.schedule_http_retry_max_delay,
    )
    telegram_session = create_telegram_session(
        proxy_url=settings.telegram_proxy_url,
        force_ipv4=settings.telegram_force_ipv4,
        request_retries=settings.telegram_request_retries,
    )
    bot = Bot(
        settings.bot_token,
        session=telegram_session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    analytics = UsageAnalytics(
        repository, await asyncio.to_thread(settings.resolve_analytics_salt),
        previous_salts=settings.analytics_previous_salts,
    )
    notifier = ScheduleNotifier(
        bot,
        repository,
        settings.admin_telegram_ids,
        analytics=analytics,
    )
    outage_tracker = ScheduleOutageTracker(
        repository,
        notifier,
        settings.schedule_page_url,
        failure_threshold=settings.schedule_outage_failure_threshold,
        timezone=settings.timezone,
    )
    updater = ScheduleUpdater(
        downloader, ExcelScheduleParser(), repository, outage_tracker=outage_tracker
    )
    update_lock = asyncio.Lock()
    pin_sync_lock = asyncio.Lock()
    grok_worker = GrokReactionWorker(bot, repository, settings)
    queue_grok_reactions = grok_worker.enqueue
    grok_task = asyncio.create_task(grok_worker.run())

    async def pin_job() -> None:
        try:
            async with pin_sync_lock:
                await sync_schedule_pins(
                    bot,
                    repository,
                    now=datetime.now(ZoneInfo(settings.timezone)),
                    stale_after_minutes=settings.schedule_stale_after_minutes,
                )
        except Exception as error:
            logger.exception("Automatic schedule pin synchronization failed")
            await notifier.notify_admins_error(
                error,
                context="обновления закреплённого расписания",
            )

    async def update_job() -> None:
        await automatic_update_job(
            updater, notifier, analytics, update_lock, pin_job,
            queue_grok_reactions=queue_grok_reactions,
        )

    async def signal_update_job() -> None:
        await automatic_update_job(
            updater, notifier, analytics, update_lock, pin_job, wait_for_lock=True,
            queue_grok_reactions=queue_grok_reactions,
        )

    async def backup_job() -> None:
        try:
            backup_path = await asyncio.to_thread(
                create_database_backup,
                repository,
                settings.backups_path,
                retention_count=settings.backup_retention_count,
            )
            logger.info("Database backup created: %s", backup_path)
        except Exception as error:
            logger.exception("Automatic database backup failed")
            await notifier.notify_admins_error(
                error,
                context="резервного копирования базы данных",
            )

    async def cleanup_job() -> None:
        try:
            async with update_lock:
                report = await asyncio.to_thread(
                    cleanup_technical_data, repository, settings.downloads_path,
                    outbox_retention_days=settings.outbox_retention_days,
                    downloads_retention_days=settings.downloads_retention_days,
                    dry_run=False, limit=100,
                )
            logger.info("Technical retention completed: %s", report)
        except Exception as error:
            logger.exception("Technical retention failed")
            await notifier.notify_admins_error(error, context="очистки технических данных")

    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    scheduler.add_job(
        update_job,
        trigger="interval",
        minutes=settings.check_interval_minutes,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        pin_job,
        trigger="interval",
        minutes=settings.pin_sync_interval_minutes,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        backup_job,
        trigger="cron",
        hour=3,
        minute=30,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        analytics.prune,
        trigger="cron",
        hour=4,
        kwargs={"retention_days": settings.analytics_retention_days},
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        cleanup_job, trigger="cron", hour=4, minute=30,
        max_instances=1, coalesce=True,
    )
    scheduler.start()

    dispatcher = Dispatcher(storage=ExpiringMemoryStorage(
        ttl_seconds=settings.fsm_state_ttl_seconds,
        max_records=settings.fsm_max_records,
    ))
    signal_worker = None
    if settings.schedule_signal_bot_username:
        listener = ScheduleSignalListener(
            settings.schedule_signal_bot_username,
            repository_async(repository).get_chat_profile,
            signal_update_job,
            chat_id=settings.schedule_signal_chat_id,
            cooldown_seconds=settings.schedule_signal_cooldown_seconds,
        )
        dispatcher.include_router(create_signal_router(listener))
        signal_worker = asyncio.create_task(listener.run())
    dispatcher.include_router(router)
    dispatcher.include_router(group_router)
    delivery_worker = asyncio.create_task(notifier.run_delivery_worker())
    startup_update = asyncio.create_task(update_job())
    startup_pin_sync = asyncio.create_task(pin_job())
    polling_active = [False]
    health_task = asyncio.create_task(run_runtime_health(
        settings.database_path.parent / "runtime-health.json",
        schema_version=repository.SCHEMA_VERSION,
        delivery_worker=delivery_worker,
        scheduler=scheduler,
        polling_active=lambda: polling_active[0],
    ))
    commands_configured = False
    try:
        while True:
            try:
                if not commands_configured:
                    await configure_commands(bot)
                    commands_configured = True
                polling_active[0] = True
                await dispatcher.start_polling(
                    bot,
                    tasks_concurrency_limit=settings.telegram_max_concurrent_updates,
                    repository=repository,
                    settings=settings,
                    updater=updater,
                    notifier=notifier,
                    analytics=analytics,
                    update_lock=update_lock,
                    pin_sync_lock=pin_sync_lock,
                    queue_grok_reactions=queue_grok_reactions,
                    close_bot_session=False,
                )
                polling_active[0] = False
                break
            except TelegramNetworkError as error:
                polling_active[0] = False
                logger.warning(
                    "Telegram is unavailable (%s). Retrying in %s seconds.",
                    error,
                    settings.telegram_retry_seconds,
                )
                await asyncio.sleep(settings.telegram_retry_seconds)
    finally:
        polling_active[0] = False
        if signal_worker is not None:
            signal_worker.cancel()
            with suppress(asyncio.CancelledError):
                await signal_worker
        health_task.cancel()
        with suppress(asyncio.CancelledError):
            await health_task
        scheduler.shutdown(wait=False)
        if not startup_update.done():
            startup_update.cancel()
        if not startup_pin_sync.done():
            startup_pin_sync.cancel()
        delivery_worker.cancel()
        with suppress(asyncio.CancelledError):
            await startup_update
        with suppress(asyncio.CancelledError):
            await startup_pin_sync
        grok_task.cancel()
        await asyncio.gather(grok_task, return_exceptions=True)
        with suppress(asyncio.CancelledError):
            await delivery_worker
        await close_card_service()
        await bot.session.close()


def run(settings: Settings) -> None:
    asyncio.run(run_bot(settings))
