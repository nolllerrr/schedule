from __future__ import annotations

import asyncio
import html
import logging
from datetime import date, datetime, timedelta
from typing import Literal, cast
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramNetworkError
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
from schedule_bot.bot.groups import router as group_router
from schedule_bot.bot.session import create_telegram_session
from schedule_bot.config import Settings
from schedule_bot.downloader import ScheduleDownloader
from schedule_bot.message_utils import answer_in_chunks
from schedule_bot.maintenance import backup_before_migration, create_database_backup
from schedule_bot.notifier import ScheduleNotifier
from schedule_bot.parser import ExcelScheduleParser
from schedule_bot.presentation import format_schedule, format_week
from schedule_bot.repository import ScheduleRepository
from schedule_bot.services import ScheduleUpdater


logger = logging.getLogger(__name__)
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
            BotCommand(command="privacy", description="Какие данные хранит бот"),
            BotCommand(command="delete_me", description="Удалить мои данные"),
        ],
        scope=BotCommandScopeAllPrivateChats(),
    )
    await bot.set_my_commands(
        [
            BotCommand(command="today", description="Расписание на сегодня"),
            BotCommand(command="tomorrow", description="Расписание на завтра"),
            BotCommand(command="week", description="Расписание на неделю"),
            BotCommand(command="next", description="Следующий учебный день"),
            BotCommand(command="settings", description="Настройки этого чата"),
            BotCommand(command="setup", description="Выбрать учебную группу"),
            BotCommand(command="notifications", description="Включить уведомления"),
            BotCommand(command="pin", description="Закреплять расписание"),
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
            [KeyboardButton(text="Сегодня"), KeyboardButton(text="Завтра")],
            [KeyboardButton(text="Неделя"), KeyboardButton(text="Следующий день")],
            [KeyboardButton(text="Выбрать дату")],
            [KeyboardButton(text=f"Уведомления: {state}")],
            [KeyboardButton(text="Сменить профиль")],
        ],
        resize_keyboard=True,
    )


def dates_keyboard(dates: list[date]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=value.strftime("%d.%m.%Y"),
                    callback_data=f"date:{value.isoformat()}",
                )
            ]
            for value in dates
        ]
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
    groups = repository.list_groups()
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
    repository.save_profile(
        callback.from_user.id, role="student", target=group
    )
    await analytics.track(
        "profile_configured",
        actor_id=callback.from_user.id,
        chat_type="private",
        properties={"role": "student"},
    )
    await callback.message.answer(
        f"Выбрана группа <b>{html.escape(group)}</b>.",
        reply_markup=main_keyboard(),
    )
    await callback.answer()


@router.message(ProfileSetup.waiting_teacher_query, F.text)
async def find_teacher(
    message: Message, repository: ScheduleRepository, state: FSMContext
) -> None:
    teachers = repository.search_teachers(message.text or "")
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
    repository.save_profile(
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
        f"Выбран преподаватель <b>{html.escape(teacher)}</b>.",
        reply_markup=main_keyboard(),
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
    scope: str,
) -> None:
    profile = repository.get_profile(message.from_user.id)
    if profile is None:
        await message.answer("Сначала выберите профиль командой /start.")
        return
    lesson_date = selected_date or (
        datetime.now(ZoneInfo(settings.timezone)).date() + timedelta(days=days)
    )
    role = cast(Literal["student", "teacher"], profile["role"])
    target = str(profile["target"])
    lessons = repository.lessons_for(
        role=role, target=target, lesson_date=lesson_date
    )
    await answer_in_chunks(
        message,
        format_schedule(
            lessons, target=target, lesson_date=lesson_date, role=role
        ),
        reply_markup=main_keyboard(bool(profile["notifications"])),
    )
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
    profile = repository.get_profile(message.from_user.id)
    if profile is None:
        await message.answer("Сначала выберите профиль командой /start.")
        return
    role = cast(Literal["student", "teacher"], profile["role"])
    target = str(profile["target"])
    start = datetime.now(ZoneInfo(settings.timezone)).date() + timedelta(days=1)
    for lesson_date in repository.available_dates(from_date=start, limit=21):
        if lesson_date < start:
            continue
        lessons = repository.lessons_for(
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
    await message.answer("Следующий учебный день пока не опубликован.")
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
    profile = repository.get_profile(message.from_user.id)
    if profile is None:
        await message.answer("Сначала выберите профиль командой /start.")
        return
    role = cast(Literal["student", "teacher"], profile["role"])
    target = str(profile["target"])
    start = datetime.now(ZoneInfo(settings.timezone)).date()
    end = start + timedelta(days=6)
    dates = [
        value
        for value in repository.available_dates(from_date=start, limit=14)
        if start <= value <= end
    ]
    schedule = {
        lesson_date: repository.lessons_for(
            role=role, target=target, lesson_date=lesson_date
        )
        for lesson_date in dates
    }
    schedule = {key: value for key, value in schedule.items() if value}
    await answer_in_chunks(
        message,
        format_week(schedule, target=target, role=role),
        reply_markup=main_keyboard(bool(profile["notifications"])),
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
    today_value = datetime.now(ZoneInfo(settings.timezone)).date()
    dates = repository.available_dates(from_date=today_value)
    if not dates:
        await message.answer("В базе пока нет расписания.")
        return
    await message.answer("Выберите дату:", reply_markup=dates_keyboard(dates))


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
    profile = repository.get_profile(callback.from_user.id)
    if profile is None:
        await callback.message.answer("Сначала выберите профиль командой /start.")
    else:
        role = cast(Literal["student", "teacher"], profile["role"])
        target = str(profile["target"])
        lessons = repository.lessons_for(
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
    profile = repository.get_profile(message.from_user.id)
    if profile is None:
        await message.answer("Сначала выберите профиль командой /start.")
        return
    enabled = not bool(profile["notifications"])
    repository.set_notifications(message.from_user.id, enabled)
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


@router.message(Command("delete_me"))
async def request_data_deletion(message: Message) -> None:
    await message.answer(
        "Удалить профиль, настройки уведомлений и связанную обезличенную "
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
) -> None:
    actor_key = analytics.actor_key("user", callback.from_user.id)
    deleted = repository.delete_user_data(callback.from_user.id, actor_key)
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
    settings: Settings,
    updater: ScheduleUpdater,
    notifier: ScheduleNotifier,
    update_lock: asyncio.Lock,
    analytics: UsageAnalytics,
) -> None:
    if message.from_user.id not in settings.admin_telegram_ids:
        await message.answer("Команда доступна только администратору.")
        return
    try:
        async with update_lock:
            result = await updater.update_once()
            await notifier.notify_import(result)
    except Exception as error:
        logger.exception("Manual schedule update failed")
        await analytics.track(
            "import_failed",
            actor_id="schedule-updater",
            actor_kind="system",
            chat_type="private",
            properties={"error_type": type(error).__name__},
        )
        await message.answer(f"Ошибка обновления: {type(error).__name__}")
        return
    await analytics.track(
        "import_completed",
        actor_id="schedule-updater",
        actor_kind="system",
        chat_type="private",
        properties={
            "skipped": result.skipped,
            "lesson_count": len(result.parsed.lessons),
            "change_count": len(result.changes),
        },
    )
    if result.skipped:
        await message.answer("Новых файлов нет.")
    else:
        await message.answer(
            f"Импорт завершён: {len(result.parsed.lessons)} занятий, "
            f"изменений — {len(result.changes)}."
        )


async def run_bot(settings: Settings) -> None:
    repository = ScheduleRepository(settings.database_path)
    migration_backup = backup_before_migration(
        repository,
        settings.backups_path,
    )
    if migration_backup:
        logger.info("Pre-migration database backup created: %s", migration_backup)
    repository.initialize()
    downloader = ScheduleDownloader(
        settings.schedule_page_url,
        settings.downloads_path,
        building=1,
    )
    updater = ScheduleUpdater(downloader, ExcelScheduleParser(), repository)
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
    analytics = UsageAnalytics(repository, settings.analytics_salt)
    notifier = ScheduleNotifier(
        bot,
        repository,
        settings.admin_telegram_ids,
        analytics=analytics,
    )
    update_lock = asyncio.Lock()

    async def update_job() -> None:
        try:
            async with update_lock:
                result = await updater.update_once()
                await notifier.notify_import(result)
            await analytics.track(
                "import_completed",
                actor_id="schedule-updater",
                actor_kind="system",
                chat_type="private",
                properties={
                    "skipped": result.skipped,
                    "lesson_count": len(result.parsed.lessons),
                    "change_count": len(result.changes),
                },
            )
        except Exception as error:
            logger.exception("Automatic schedule update failed")
            await analytics.track(
                "import_failed",
                actor_id="schedule-updater",
                actor_kind="system",
                chat_type="private",
                properties={"error_type": type(error).__name__},
            )
            await notifier.notify_admins_error(error)

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

    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    scheduler.add_job(
        update_job,
        trigger="interval",
        minutes=settings.check_interval_minutes,
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
    scheduler.start()

    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    dispatcher.include_router(group_router)
    startup_update = asyncio.create_task(update_job())
    commands_configured = False
    try:
        while True:
            try:
                if not commands_configured:
                    await configure_commands(bot)
                    commands_configured = True
                await dispatcher.start_polling(
                    bot,
                    repository=repository,
                    settings=settings,
                    updater=updater,
                    notifier=notifier,
                    analytics=analytics,
                    update_lock=update_lock,
                    close_bot_session=False,
                )
                break
            except TelegramNetworkError as error:
                logger.warning(
                    "Telegram is unavailable (%s). Retrying in %s seconds.",
                    error,
                    settings.telegram_retry_seconds,
                )
                await asyncio.sleep(settings.telegram_retry_seconds)
    finally:
        scheduler.shutdown(wait=False)
        if not startup_update.done():
            startup_update.cancel()
        await bot.session.close()


def run(settings: Settings) -> None:
    asyncio.run(run_bot(settings))
