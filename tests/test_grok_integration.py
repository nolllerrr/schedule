from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from schedule_bot import config, reactions
from schedule_bot.bot.app import automatic_update_job, manual_update
from schedule_bot.domain import ImportResult, Lesson, ParsedSchedule
from schedule_bot.notifier import ScheduleNotifier
from schedule_bot.parser import ExcelScheduleParser
from schedule_bot.repository import ScheduleRepository
from schedule_grok import generate_grok_reaction, load_config


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setattr(config, "_load_dotenv", lambda: None)
    for name in ("GROK_REACTIONS_ENABLED", "GROK_CLI_PATH", "GROK_TIMEOUT_SECONDS",
                 "GROK_REACTION_CHAT_TYPES", "GROK_REACTION_CHAT_IDS", "GROK_REACTION_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    return config.Settings.from_env(require_bot_token=False)



@pytest.fixture
def grok_system(settings, tmp_path):
    repo = ScheduleRepository(tmp_path / "grok.db")
    for chat in (-1, -2):
        repo.save_chat_profile(chat, chat_type="group", chat_title="Test", target="A", configured_by=10)
    day = date(2026, 10, 20)
    lesson = Lesson(day, "A", 1, "1 пара", 1, "09:00", "10:30", "Math")
    result = repo.import_schedule(ParsedSchedule("20.10.2026.xlsx", day, day, (day,), (lesson,)), sha256="test")
    with repo._connect() as connection:
        connection.execute("UPDATE notification_outbox SET status='sent'")
        connection.commit()
    return SimpleNamespace(repo=repo, result=result,
        settings=replace(settings, grok_reactions_enabled=True, grok_reaction_chat_ids=(-1, -2)),
        bot=SimpleNamespace(send_message=AsyncMock()))

def new_import() -> ImportResult:
    return ImportResult(1, False, (), None, new_dates=(date(2026, 10, 19),))


def test_imported_config_is_available_and_runtime_remains_opt_in(settings, monkeypatch):
    bundled = load_config()
    assert bundled.max_reaction_chars == 240
    assert "{schedule_diff}" in bundled.prompt_template
    assert settings.grok_reactions_enabled is False
    assert settings.grok_cli_path == "grok"
    assert settings.grok_reaction_chat_ids == ()
    monkeypatch.setenv("GROK_REACTIONS_ENABLED", "true")
    monkeypatch.setenv("GROK_CLI_PATH", "local-grok")
    monkeypatch.setenv("GROK_TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv("GROK_REACTION_CHAT_TYPES", "supergroup, group")
    monkeypatch.setenv("GROK_REACTION_CHAT_IDS", "-1, -2, -1")
    enabled = config.Settings.from_env(require_bot_token=False)
    assert enabled.grok_reactions_enabled is True
    assert enabled.grok_cli_path == "local-grok"
    assert enabled.grok_timeout_seconds == 12.5
    assert enabled.grok_reaction_chat_types == ("supergroup", "group")
    assert enabled.grok_reaction_chat_ids == (-1, -2)


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "invalid"])
def test_invalid_grok_timeout_is_rejected(settings, monkeypatch, value):
    monkeypatch.setenv("GROK_TIMEOUT_SECONDS", value)
    with pytest.raises(ValueError, match="GROK_TIMEOUT_SECONDS"):
        config.Settings.from_env(require_bot_token=False)


@pytest.mark.asyncio
async def test_generator_with_fake_cli_returns_cleaned_bounded_text(settings, tmp_path):
    fake = tmp_path / "fake_cli.py"
    fake.write_text('import json; print(json.dumps({"response": "«реакция на расписание»"}))', encoding="utf-8")
    override = tmp_path / "config.json"
    override.write_text(json.dumps({"cli_args": [str(fake)], "max_reaction_chars": 12}), encoding="utf-8")
    text = await generate_grok_reaction(
        "Новое расписание", cli_path=sys.executable, timeout=5,
        enabled=True, config_path=override,
    )
    assert text == "реакция на р"


@pytest.mark.asyncio
async def test_missing_cli_is_a_nonfatal_failure(settings, tmp_path):
    assert await generate_grok_reaction(
        "Новое расписание", enabled=True, cli_path=str(tmp_path / "missing-grok")
    ) is None


@pytest.mark.asyncio
async def test_cli_timeout_is_nonfatal_and_process_is_reaped(settings, tmp_path):
    fake = tmp_path / "slow_cli.py"
    fake.write_text("import time; time.sleep(5)", encoding="utf-8")
    override = tmp_path / "config.json"
    override.write_text(json.dumps({"cli_args": [str(fake)]}), encoding="utf-8")
    assert await generate_grok_reaction(
        "Новое расписание", enabled=True, cli_path=sys.executable,
        timeout=0.05, config_path=override,
    ) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [
    ImportResult(1, True, (), None),
    ImportResult(None, False, (), None),
    ImportResult(1, False, (), None),
])
async def test_unchanged_imports_never_generate_or_send(settings, monkeypatch, result):
    generate = AsyncMock()
    monkeypatch.setattr(reactions, "generate_grok_reaction", generate)
    bot = SimpleNamespace(send_message=AsyncMock())
    await reactions.dispatch_grok_reactions(
        bot, None, replace(settings, grok_reactions_enabled=True, grok_reaction_chat_ids=(-1,)), result
    )
    generate.assert_not_awaited()
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_runtime_overrides_enabled_module_config(settings, monkeypatch):
    generate = AsyncMock()
    monkeypatch.setattr(reactions, "generate_grok_reaction", generate)
    await reactions.dispatch_grok_reactions(
        None, None, replace(settings, grok_reaction_chat_ids=(-1,)), new_import()
    )
    generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_reaction_is_generated_once_for_matching_groups(grok_system, monkeypatch):
    s = grok_system
    generate = AsyncMock(return_value="reaction")
    monkeypatch.setattr(reactions, "generate_grok_reaction", generate)
    await reactions.dispatch_grok_reactions(s.bot, s.repo, s.settings, s.result)
    generate.assert_awaited_once()
    assert {call.args[0] for call in s.bot.send_message.call_args_list} == {-1, -2}
    assert all(call.kwargs == {"parse_mode": None} for call in s.bot.send_message.call_args_list)


@pytest.mark.asyncio
async def test_server_adapter_reads_the_current_local_outbox(settings, tmp_path, schedule_workbook, monkeypatch):
    repository = ScheduleRepository(tmp_path / "schedule.db")
    repository.save_chat_profile(-1, chat_type="group", chat_title="Local", target="ПД-12", configured_by=10)
    parsed = ExcelScheduleParser().parse(schedule_workbook)
    result = repository.import_schedule(parsed, sha256="grok-local-test")
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)))
    notifier = ScheduleNotifier(bot, repository, (), broadcast_interval=0, group_interval=0)
    await notifier.notify_import(result)
    rows = reactions._get_import_deliveries(repository, result.import_id, replace(settings, grok_reaction_chat_ids=(-1,)))
    assert rows and all(row["destination_id"] == -1 for row in rows)
    assert all("Расписание" in row["text"] or "расписание" in row["text"] for row in rows)
    generate = AsyncMock(return_value="комментарий")
    monkeypatch.setattr(reactions, "generate_grok_reaction", generate)
    await notifier.deliver_pending()
    bot.send_message.reset_mock()
    await reactions.dispatch_grok_reactions(
        bot, repository, replace(settings, grok_reactions_enabled=True, grok_reaction_chat_ids=(-1,)), result
    )
    assert generate.await_count >= 1
    assert bot.send_message.await_count >= 1


@pytest.mark.asyncio
async def test_automatic_and_manual_updates_keep_the_reaction_hook(settings, monkeypatch):
    result = new_import()
    updater = SimpleNamespace(update_once=AsyncMock(return_value=result))
    notifier = SimpleNamespace(notify_import=AsyncMock(), notify_admins_error=AsyncMock())
    analytics = SimpleNamespace(track=AsyncMock())
    queue = Mock()
    await automatic_update_job(
        updater, notifier, analytics, asyncio.Lock(), AsyncMock(), queue_grok_reactions=queue
    )
    queue.assert_called_once_with(result)
    queue.reset_mock()
    monkeypatch.setattr("schedule_bot.bot.app.sync_schedule_pins", AsyncMock())
    message = SimpleNamespace(from_user=SimpleNamespace(id=10), answer=AsyncMock())
    await manual_update(
        message, None, replace(settings, admin_telegram_ids=(10,)), None,
        updater, notifier, asyncio.Lock(), analytics, queue_grok_reactions=queue,
    )
    queue.assert_called_once_with(result)
    assert message.answer.await_count == 1


@pytest.mark.asyncio
async def test_runtime_background_reactions_are_cancelled_on_shutdown(settings, tmp_path, monkeypatch):
    from schedule_bot.bot import app

    settings = replace(settings, grok_reactions_enabled=True, grok_reaction_chat_ids=(-1,), database_path=tmp_path / "runtime.db",
                       backups_path=tmp_path / "backups")
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def reaction(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def polling(bot, **kwargs):
        kwargs["queue_grok_reactions"](new_import())
        await asyncio.wait_for(started.wait(), 1)

    dispatcher = SimpleNamespace(include_router=Mock(), start_polling=polling)
    monkeypatch.setattr(app, "Dispatcher", Mock(return_value=dispatcher))
    monkeypatch.setattr(app, "Bot", Mock(return_value=SimpleNamespace(session=SimpleNamespace(close=AsyncMock()))))
    monkeypatch.setattr(app, "configure_commands", AsyncMock())
    monkeypatch.setattr(app, "automatic_update_job", AsyncMock())
    monkeypatch.setattr(app, "sync_schedule_pins", AsyncMock())
    monkeypatch.setattr(app.ScheduleNotifier, "run_delivery_worker", AsyncMock())
    monkeypatch.setattr(reactions, "_dispatch_import", reaction)
    await app.run_bot(settings)
    assert cancelled.is_set()


@pytest.mark.parametrize("value", ["0", "123", "-1,invalid", "1.5", "-1;-2"])
def test_invalid_allowed_chat_ids_are_rejected(settings, monkeypatch, value):
    monkeypatch.setenv("GROK_REACTION_CHAT_IDS", value)
    with pytest.raises(ValueError, match="GROK_REACTION_CHAT_IDS"):
        config.Settings.from_env(require_bot_token=False)


@pytest.mark.asyncio
async def test_empty_allowlist_disables_generation_before_reading_deliveries(settings, monkeypatch):
    deliveries = Mock()
    generate = AsyncMock()
    monkeypatch.setattr(reactions, "_get_import_deliveries", deliveries)
    monkeypatch.setattr(reactions, "generate_grok_reaction", generate)
    bot = SimpleNamespace(send_message=AsyncMock())
    await reactions.dispatch_grok_reactions(
        bot, None, replace(settings, grok_reactions_enabled=True), new_import()
    )
    deliveries.assert_not_called()
    generate.assert_not_awaited()
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed_ids,expected", [((-1,), 1), ((-9,), 0)])
async def test_only_allowed_chat_generates_a_reaction(grok_system, monkeypatch, allowed_ids, expected):
    s = grok_system
    generate = AsyncMock(return_value="reaction")
    monkeypatch.setattr(reactions, "generate_grok_reaction", generate)
    await reactions.dispatch_grok_reactions(s.bot, s.repo,
        replace(s.settings, grok_reaction_chat_ids=allowed_ids), s.result)
    assert generate.await_count == expected
    assert s.bot.send_message.await_count == expected
    if expected:
        s.bot.send_message.assert_awaited_once_with(-1, "reaction", parse_mode=None)
