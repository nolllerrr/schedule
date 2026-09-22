from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Literal, cast
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from schedule_bot.config import Settings
from schedule_bot.downloader import ScheduleDownloader
from schedule_bot.notifier import ScheduleNotifier
from schedule_bot.parser import ExcelScheduleParser
from schedule_bot.repository import ScheduleRepository
from schedule_bot.services import ScheduleUpdater, format_schedule


logger = logging.getLogger(__name__)
router = Router()


class ProfileSetup(StatesGroup):
    waiting_teacher_query = State()
    choosing_teacher = State()


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
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
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
    callback: CallbackQuery, repository: ScheduleRepository
) -> None:
    group = callback.data.split(":", 1)[1]
    repository.save_profile(
        callback.from_user.id, role="student", target=group
    )
    await callback.message.answer(
        f"Выбрана группа <b>{group}</b>.", reply_markup=main_keyboard()
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
    await state.clear()
    await callback.message.answer(
        f"Выбран преподаватель <b>{teacher}</b>.",
        reply_markup=main_keyboard(),
    )
    await callback.answer()


async def _send_schedule(
    message: Message,
    repository: ScheduleRepository,
    settings: Settings,
    *,
    days: int = 0,
    selected_date: date | None = None,
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
    await message.answer(
        format_schedule(
            lessons, target=target, lesson_date=lesson_date, role=role
        ),
        reply_markup=main_keyboard(bool(profile["notifications"])),
    )


@router.message(F.text == "Сегодня")
async def today(
    message: Message, repository: ScheduleRepository, settings: Settings
) -> None:
    await _send_schedule(message, repository, settings, days=0)


@router.message(F.text == "Завтра")
async def tomorrow(
    message: Message, repository: ScheduleRepository, settings: Settings
) -> None:
    await _send_schedule(message, repository, settings, days=1)


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
        await callback.message.answer(
            format_schedule(
                lessons,
                target=target,
                lesson_date=lesson_date,
                role=role,
            ),
            reply_markup=main_keyboard(bool(profile["notifications"])),
        )
    await callback.answer()


@router.message(F.text.startswith("Уведомления:"))
async def toggle_notifications(
    message: Message, repository: ScheduleRepository
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


@router.message(F.text == "Сменить профиль")
async def change_profile(message: Message, state: FSMContext) -> None:
    await start(message, state)


@router.message(Command("update"))
async def manual_update(
    message: Message,
    settings: Settings,
    updater: ScheduleUpdater,
    notifier: ScheduleNotifier,
) -> None:
    if message.from_user.id not in settings.admin_telegram_ids:
        await message.answer("Команда доступна только администратору.")
        return
    try:
        result = await updater.update_once()
        await notifier.notify_import(result)
    except Exception as error:
        logger.exception("Manual schedule update failed")
        await message.answer(f"Ошибка обновления: {type(error).__name__}")
        return
    if result.skipped:
        await message.answer("Новых файлов нет.")
    else:
        await message.answer(
            f"Импорт завершён: {len(result.parsed.lessons)} занятий, "
            f"изменений — {len(result.changes)}."
        )


async def run_bot(settings: Settings) -> None:
    repository = ScheduleRepository(settings.database_path)
    repository.initialize()
    downloader = ScheduleDownloader(
        settings.schedule_page_url,
        settings.downloads_path,
        building=1,
    )
    updater = ScheduleUpdater(downloader, ExcelScheduleParser(), repository)
    telegram_session = (
        AiohttpSession(proxy=settings.telegram_proxy_url)
        if settings.telegram_proxy_url
        else AiohttpSession()
    )
    bot = Bot(
        settings.bot_token,
        session=telegram_session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    notifier = ScheduleNotifier(bot, repository, settings.admin_telegram_ids)

    async def update_job() -> None:
        try:
            result = await updater.update_once()
            await notifier.notify_import(result)
        except Exception as error:
            logger.exception("Automatic schedule update failed")
            await notifier.notify_admins_error(error)

    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    scheduler.add_job(
        update_job,
        trigger="interval",
        minutes=settings.check_interval_minutes,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()

    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    await update_job()
    try:
        while True:
            try:
                await dispatcher.start_polling(
                    bot,
                    repository=repository,
                    settings=settings,
                    updater=updater,
                    notifier=notifier,
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
        await bot.session.close()


def run(settings: Settings) -> None:
    asyncio.run(run_bot(settings))
