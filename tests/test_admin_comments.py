import asyncio
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from schedule_bot.bot import app, comments
from schedule_bot.domain import Lesson, ParsedSchedule
from schedule_bot.repository import ScheduleRepository


DAY = date(2026, 10, 28)


@pytest.fixture
def system(tmp_path, monkeypatch):
    repository = ScheduleRepository(tmp_path / "schedule.db")
    lessons = tuple(
        Lesson(DAY, group, 1, "1 пара", 1, "09:00", "10:30", subject)
        for group, subject in (("ИС-22", "Математика"), ("ПД-12", "Право"))
    )
    repository.import_schedule(
        ParsedSchedule("28.10.2026.xlsx", DAY, DAY, (DAY,), lessons), sha256="comments",
    )
    repository.save_profile(42, role="student", target="ИС-22")
    for chat_id, target in ((-1001, "ИС-22"), (-1002, "ПД-12")):
        repository.save_chat_profile(
            chat_id, chat_type="supergroup", chat_title=f"Чат {target}",
            target=target, configured_by=42,
        )
    settings = SimpleNamespace(
        admin_telegram_ids=(42,), timezone="Europe/Moscow",
        grok_reactions_enabled=True, grok_cli_path="test-grok", grok_timeout_seconds=1,
        grok_reaction_chat_ids=(-1001, -1002),
        grok_reaction_chat_types=("group", "supergroup"),
    )
    class Clock:
        @staticmethod
        def now(tz):
            assert tz == ZoneInfo(settings.timezone)
            return datetime(2026, 10, 28, 8, tzinfo=tz)

    monkeypatch.setattr(comments, "datetime", Clock)
    generate = AsyncMock(side_effect=["первый <комментарий> & текст", "второй комментарий"])
    monkeypatch.setattr(comments, "generate_grok_reaction", generate)
    callback = SimpleNamespace(
        data="pnav:42:new_comment", from_user=SimpleNamespace(id=42), answer=AsyncMock(),
        bot=SimpleNamespace(send_message=AsyncMock()),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=42, type="private"), answer=AsyncMock(), edit_text=AsyncMock(),
        ),
    )
    return SimpleNamespace(repo=repository, settings=settings, callback=callback, generate=generate)


async def click(system):
    await app.private_menu_action(system.callback, system.repo, system.settings, analytics=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id,role,visible", [(42, "student", True), (7, "student", False), (42, "teacher", False)])
async def test_button_is_visible_only_to_bot_admin_with_selected_group(system, user_id, role, visible):
    system.repo.save_profile(user_id, role=role, target="ИС-22")
    message = SimpleNamespace(from_user=SimpleNamespace(id=user_id), answer=AsyncMock())
    await app.open_private_menu(message, system.repo, system.settings)
    keyboard = message.answer.call_args.kwargs["reply_markup"]
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert (f"pnav:{user_id}:new_comment" in callbacks) is visible

    system.callback.from_user.id = user_id
    system.callback.data = f"pnav:{user_id}:root"
    await click(system)
    keyboard = system.callback.message.edit_text.call_args.kwargs["reply_markup"]
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert (f"pnav:{user_id}:new_comment" in callbacks) is visible


@pytest.mark.asyncio
async def test_comment_regenerates_and_is_sent_only_to_the_selected_groups_chat(system):
    async def generation(*args, **kwargs):
        system.callback.answer.assert_awaited()
        return "первый <комментарий> & текст" if "ИС-22" in args[0] else "второй комментарий"

    system.generate.side_effect = generation
    await click(system)
    await click(system)
    system.repo.save_profile(42, role="student", target="ПД-12")
    await click(system)
    assert system.generate.await_count == 3
    first, repeated, second = system.generate.call_args_list
    assert repeated == first
    assert "ИС-22" in first.args[0] and "Математика" in first.args[0]
    assert "ПД-12" not in first.args[0] and "Право" not in first.args[0]
    assert "ПД-12" in second.args[0] and "Право" in second.args[0]
    assert "<b>" not in first.args[0]
    assert first.kwargs == {"enabled": True, "cli_path": "test-grok", "timeout": 1}
    sends = system.callback.bot.send_message.call_args_list
    assert [call.args[0] for call in sends] == [-1001, -1001, -1002]
    assert sends[0].args[1] == "первый <комментарий> & текст"
    assert sends[2].args[1] == "второй комментарий"
    assert all(call.kwargs == {"parse_mode": None} for call in sends)
    texts = [call.args[0] for call in system.callback.message.answer.call_args_list]
    assert "Комментарий отправлен" in texts[0] and "ИС-22 · 28.10.2026" in texts[0]
    assert "Комментарий отправлен" in texts[2] and "ПД-12 · 28.10.2026" in texts[2]
    assert all("первый" not in text and "второй" not in text for text in texts)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["non_admin", "wrong_owner", "teacher", "disabled", "no_profile", "group_chat"])
async def test_forged_or_unavailable_action_never_starts_generator(system, reason):
    if reason == "non_admin":
        system.settings.admin_telegram_ids = (99,)
    elif reason == "wrong_owner":
        system.callback.from_user.id = 99
    elif reason == "teacher":
        system.repo.save_profile(42, role="teacher", target="Иванов")
    elif reason == "disabled":
        system.settings.grok_reactions_enabled = False
    elif reason == "no_profile":
        system.callback.from_user.id = 99
        system.callback.data = "pnav:99:new_comment"
    else:
        system.callback.message.chat.type = "supergroup"
    await click(system)
    system.generate.assert_not_awaited()
    assert system.callback.answer.call_args.kwargs["show_alert"] is True
    system.callback.message.answer.assert_not_awaited()
    system.callback.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_current_schedule_explains_why_no_comment_was_generated(system):
    system.repo.save_profile(42, role="student", target="Нет занятий")
    system.repo.save_chat_profile(
        -1001, chat_type="supergroup", chat_title="Пустая группа", target="Нет занятий", configured_by=42,
    )
    await click(system)
    system.generate.assert_not_awaited()
    assert "нет актуального расписания" in system.callback.message.answer.call_args.args[0]
    system.callback.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, RuntimeError("private CLI details"), "timeout"])
async def test_generation_failure_is_safe_and_allows_retry(system, failure, caplog):
    if failure == "timeout":
        system.settings.grok_timeout_seconds = 0.01

        async def slow(*args, **kwargs):
            await asyncio.Event().wait()

        system.generate.side_effect = slow
    elif isinstance(failure, Exception):
        system.generate.side_effect = failure
    else:
        system.generate.side_effect = None
        system.generate.return_value = None
    await click(system)
    assert "Не удалось сгенерировать" in system.callback.message.answer.call_args.args[0]
    assert "private CLI details" not in caplog.text
    system.callback.bot.send_message.assert_not_awaited()
    system.generate.side_effect = None
    system.generate.return_value = "повтор удался"
    await click(system)
    assert "Комментарий отправлен" in system.callback.message.answer.call_args.args[0]
    assert system.callback.bot.send_message.call_args.args == (-1001, "повтор удался")


@pytest.mark.asyncio
async def test_concurrent_click_is_rejected_and_cancellation_releases_slot(system):
    started = asyncio.Event()

    async def slow(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    system.generate.side_effect = slow
    task = asyncio.create_task(click(system))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        await click(system)
        assert "уже генерируется" in system.callback.answer.call_args.args[0]
        system.generate.assert_awaited_once()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    system.generate.side_effect = None
    system.generate.return_value = "после отмены"
    await click(system)
    assert "Комментарий отправлен" in system.callback.message.answer.call_args.args[0]
    assert system.callback.bot.send_message.call_args.args == (-1001, "после отмены")


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["empty_allowlist", "wrong_group", "inactive", "notifications_off", "wrong_type"])
async def test_unavailable_destinations_are_rejected_before_generation(system, reason):
    if reason == "empty_allowlist":
        system.settings.grok_reaction_chat_ids = ()
    elif reason == "wrong_group":
        system.settings.grok_reaction_chat_ids = (-1002,)
    elif reason == "inactive":
        system.repo.deactivate_chat(-1001)
    elif reason == "notifications_off":
        system.repo.set_chat_notifications(-1001, False)
    else:
        system.settings.grok_reaction_chat_types = ("group",)
    await click(system)
    system.generate.assert_not_awaited()
    system.callback.bot.send_message.assert_not_awaited()
    assert "Нет доступного чата" in system.callback.answer.call_args.args[0]


@pytest.mark.asyncio
async def test_multiple_matching_chats_require_destination_choice(system):
    system.repo.save_chat_profile(
        -1003, chat_type="supergroup", chat_title="Другой чат ИС-22", target="ИС-22", configured_by=42,
    )
    system.settings.grok_reaction_chat_ids += (-1003,)
    await click(system)
    system.generate.assert_not_awaited()
    system.callback.bot.send_message.assert_not_awaited()
    keyboard = system.callback.message.answer.call_args.kwargs["reply_markup"]
    callbacks = {button.callback_data for row in keyboard.inline_keyboard for button in row}
    assert callbacks == {"pnav:42:new_comment_-1001", "pnav:42:new_comment_-1003"}
    assert all(len(value.encode()) <= 64 for value in callbacks)
    system.callback.data = "pnav:42:new_comment_-1003"
    await click(system)
    system.generate.assert_awaited_once()
    system.callback.bot.send_message.assert_awaited_once_with(
        -1003, "первый <комментарий> & текст", parse_mode=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("destination", ["-1002", "-9999", "42", "invalid"])
async def test_forged_destination_cannot_redirect_a_comment(system, destination):
    system.callback.data = f"pnav:42:new_comment_{destination}"
    await click(system)
    system.generate.assert_not_awaited()
    system.callback.bot.send_message.assert_not_awaited()
    assert "Чат больше недоступен" in system.callback.answer.call_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["target", "notifications", "inactive", "allowlist", "enabled"])
async def test_destination_is_rechecked_after_generation(system, change):
    async def generate(*args, **kwargs):
        if change == "target":
            system.repo.save_chat_profile(
                -1001, chat_type="supergroup", chat_title="Другой профиль", target="ПД-12", configured_by=42,
            )
        elif change == "notifications":
            system.repo.set_chat_notifications(-1001, False)
        elif change == "inactive":
            system.repo.deactivate_chat(-1001)
        elif change == "allowlist":
            system.settings.grok_reaction_chat_ids = ()
        else:
            system.settings.grok_reactions_enabled = False
        return "уже не актуально"

    system.generate.side_effect = generate
    await click(system)
    system.generate.assert_awaited_once()
    system.callback.bot.send_message.assert_not_awaited()
    assert "Комментарий не отправлен" in system.callback.message.answer.call_args.args[0]


@pytest.mark.asyncio
async def test_delivery_failure_does_not_report_success_or_retry(system, caplog):
    system.callback.bot.send_message.side_effect = RuntimeError("private delivery details")
    await click(system)
    system.generate.assert_awaited_once()
    system.callback.bot.send_message.assert_awaited_once()
    assert "Не удалось отправить комментарий" in system.callback.message.answer.call_args.args[0]
    assert "private delivery details" not in caplog.text
