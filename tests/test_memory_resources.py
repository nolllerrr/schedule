from __future__ import annotations

import asyncio
import gc
import hashlib
import os
import subprocess
import sys
import tracemalloc
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

pytestmark = pytest.mark.usefixtures("text_schedule_media")
from aiogram.exceptions import TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import SendMessage

from schedule_bot.bot.storage import ExpiringMemoryStorage
from schedule_bot.domain import Lesson, ParsedSchedule
from schedule_bot.import_validation import ImportQuarantinedError
from schedule_bot.notifier import (
    ScheduleNotifier,
    _destination_lock,
    _destination_locks,
)
from schedule_bot.parser import ExcelScheduleParser
from schedule_bot.pinning import _chat_pin_lock, _pin_locks
from schedule_bot.repository import ScheduleRepository
from schedule_bot.services import ScheduleUpdater


@pytest.mark.asyncio
async def test_known_download_skips_parsing_even_after_restart(
    tmp_path, schedule_workbook
):
    repo = ScheduleRepository(tmp_path / "test.db")
    downloaded = SimpleNamespace(
        path=schedule_workbook,
        sha256=hashlib.sha256(schedule_workbook.read_bytes()).hexdigest(),
        link=SimpleNamespace(
            filename=schedule_workbook.name, url="https://source.test/file.xlsx"
        ),
    )
    source = SimpleNamespace(fetch_latest=AsyncMock(return_value=downloaded))
    tracker = SimpleNamespace(record_success=AsyncMock())
    parser = Mock(wraps=ExcelScheduleParser())
    first = await ScheduleUpdater(
        source, parser, repo, outage_tracker=tracker
    ).update_once()
    assert not first.skipped and first.parsed is not None
    second_parser = Mock()
    second_parser.parse.side_effect = AssertionError("Repeated parse")
    updater = ScheduleUpdater(source, second_parser, repo, outage_tracker=tracker)
    second = await updater.update_once()
    assert second.skipped and second.parsed is None
    assert second.import_id == first.import_id
    assert (
        second.filename == first.filename and second.lesson_count == first.lesson_count
    )
    assert tracker.record_success.await_count == 2
    second_parser.parse.assert_not_called()
    assert updater.import_local(schedule_workbook).skipped
    with pytest.raises(ValueError, match="non-empty"):
        updater.import_local(schedule_workbook, approval_reason=" ")


@pytest.mark.asyncio
async def test_unknown_fsm_reads_do_not_create_records_and_clear_removes_data():
    storage = ExpiringMemoryStorage()
    try:
        for i in range(10000):
            key = StorageKey(bot_id=1, chat_id=i, user_id=i)
            assert await storage.get_state(key) is None
            assert await storage.get_data(key) == {}
        assert not storage.storage
        context = FSMContext(storage, key)
        await context.set_state("choosing_teacher")
        await context.update_data(teacher_matches=["Иванов И.И."])
        assert (await context.get_data())["teacher_matches"] == ["Иванов И.И."]
        await context.clear()
        assert not storage.storage
        assert storage._cleanup is None
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_fsm_idle_records_expire_without_new_messages():
    storage = ExpiringMemoryStorage(ttl_seconds=0.01)
    try:
        key = StorageKey(bot_id=1, chat_id=1, user_id=1)
        await storage.set_state(key, "waiting")
        await asyncio.sleep(0.04)
        assert not storage.storage
        assert storage._cleanup is None
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_fsm_capacity_eviction_and_sliding_expiry():
    now = [0.0]
    storage = ExpiringMemoryStorage(ttl_seconds=10, max_records=2, clock=lambda: now[0])
    a, b, c = [StorageKey(bot_id=1, chat_id=i, user_id=i) for i in range(3)]
    try:
        await storage.set_state(a, "a")
        now[0] = 1
        await storage.set_state(b, "b")
        now[0] = 2
        assert await storage.get_state(a) == "a"
        await storage.set_state(c, "c")
        assert await storage.get_state(b) is None
        now[0] = 11
        assert await storage.get_state(a) == "a"
        now[0] = 13
        assert await storage.get_state(c) is None
        now[0] = 22
        assert await storage.get_state(a) is None
        assert not storage.storage
    finally:
        await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["destination", "pin"])
async def test_weak_locks_keep_waiters_serialized_then_disappear(tmp_path, kind):
    repo = ScheduleRepository(tmp_path / "unused.db")
    registry = _destination_locks if kind == "destination" else _pin_locks
    get_lock = (
        (lambda: _destination_lock(repo, "user", 123))
        if kind == "destination"
        else (lambda: _chat_pin_lock(repo, 123))
    )
    first = get_lock()
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait():
        async with get_lock():
            entered.set()
            await release.wait()

    async with first:
        waiter = asyncio.create_task(wait())
        await asyncio.sleep(0)
        gc.collect()
        assert get_lock() is first
        assert not entered.is_set()
    await asyncio.wait_for(entered.wait(), 1)
    assert get_lock() is first
    release.set()
    await waiter
    del first, waiter
    gc.collect()
    assert len(registry[asyncio.get_running_loop()]) == 0


@pytest.mark.asyncio
async def test_rate_limit_records_are_pruned_after_expiry(tmp_path, monkeypatch):
    now = [0.0]
    monkeypatch.setattr("schedule_bot.notifier.monotonic", lambda: now[0])
    notifier = ScheduleNotifier(
        None, ScheduleRepository(tmp_path / "unused.db"), (), broadcast_interval=0
    )
    for i in range(1000):
        await notifier._wait_send_slot("user", i)
    assert len(notifier._next_destination_at) == 1000
    now[0] = 2
    notifier._prune_send_slots()
    assert not notifier._next_destination_at and not notifier._destination_expirations


def prepared_repo(tmp_path, count=30):
    repo = ScheduleRepository(tmp_path / "test.db")
    for i in range(count):
        repo.save_profile(i, role="student", target="ИС-22")
    day = date(2026, 10, 15)
    repo.import_schedule(
        ParsedSchedule(
            "15.10.2026.xlsx",
            day,
            day,
            (day,),
            (Lesson(day, "ИС-22", 1, "1 пара", 1, "09:00", "10:30", "Математика"),),
        ),
        sha256="synthetic",
    )
    return repo


@pytest.mark.asyncio
async def test_failed_deliveries_share_attempt_budget_with_admin_queue(tmp_path):
    repo = prepared_repo(tmp_path)
    repo.enqueue_admin_error("critical", (100, 101))
    calls = []

    async def fail(chat_id, text, **kwargs):
        calls.append(chat_id)
        raise TelegramForbiddenError(
            method=SendMessage(chat_id=chat_id, text=text), message="blocked"
        )

    notifier = ScheduleNotifier(
        SimpleNamespace(send_message=fail),
        repo,
        (),
        broadcast_interval=0,
        private_interval=0,
        group_interval=0,
    )
    assert await notifier.deliver_pending(limit=5) == 0
    assert len(calls) == 5 and calls[:2] == [100, 101]
    assert repo.notification_outbox_counts() == {"failed": 3, "pending": 27}
    assert notifier._pending_event.is_set()
    assert await notifier.deliver_pending(limit=5) == 0
    assert len(calls) == 10
    assert repo.notification_outbox_counts() == {"failed": 8, "pending": 22}


@pytest.mark.asyncio
async def test_admin_only_delivery_has_attempt_limit(tmp_path):
    repo = ScheduleRepository(tmp_path / "test.db")
    repo.enqueue_admin_error("critical", tuple(range(30)))
    calls = []

    async def fail(chat_id, text, **kwargs):
        calls.append(chat_id)
        raise TelegramForbiddenError(
            method=SendMessage(chat_id=chat_id, text=text), message="blocked"
        )

    notifier = ScheduleNotifier(
        SimpleNamespace(send_message=fail),
        repo,
        (),
        broadcast_interval=0,
        private_interval=0,
    )
    assert await notifier.deliver_admin_pending(limit=3) == 0
    assert len(calls) == 3
    assert len(repo.pending_admin_notifications()) == 27


def test_outbox_preparation_does_not_allocate_per_recipient_list():
    class Connection:
        count = 0

        def executemany(self, sql, rows):
            for row in rows:
                self.count += 1

    connection = Connection()
    rows = (
        {
            "import_id": 1,
            "destination_id": i,
            "destination_kind": "user",
            "chat_type": "private",
            "chunk_index": 0,
            "text": "common text",
        }
        for i in range(100000)
    )
    gc.collect()
    tracemalloc.start()
    try:
        ScheduleRepository._insert_notifications(
            connection, rows, datetime.now(timezone.utc).isoformat()
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert connection.count == 100000
    assert peak < 4 * 1024 * 1024


def test_stats_preserve_malformed_json_and_actor_semantics(tmp_path):
    repo = ScheduleRepository(tmp_path / "test.db")
    repo.initialize()
    now = datetime.now(timezone.utc)
    values = [
        ("a", "schedule_requested", '{"scope":"today","result":"found"}'),
        (
            "a",
            "schedule_requested",
            '{"_actor_kind":"user","scope":"week","result":"empty"}',
        ),
        (
            "chat",
            "schedule_requested",
            '{"_actor_kind":"chat","scope":"week","result":"found"}',
        ),
        ("bad", "broken", "invalid JSON"),
        ("array", "broken", "[]"),
        ("scalar", "broken", '"text"'),
        ("b", "schedule_requested", '{"_actor_kind":null,"scope":1,"result":true}'),
    ]
    with repo._connect() as c:
        c.executemany(
            "INSERT INTO usage_events(timestamp,actor_key,chat_type,event_type,properties_json) VALUES (?,?,'private',?,?)",
            ((now.isoformat(), *v) for v in values),
        )
        c.execute(
            "INSERT INTO usage_events(timestamp,actor_key,chat_type,event_type) VALUES (?, 'old', 'private', 'old_event')",
            ((now - timedelta(days=2)).isoformat(),),
        )
        c.commit()
    result = repo.usage_stats(now)
    assert result["active_actors"] == 3  # a, malformed JSON -> empty properties, b
    assert result["events_by_type"] == {"broken": 3, "schedule_requested": 4}
    assert result["schedule_scopes"] == {"today": 1, "week": 2}
    assert result["schedule_results"] == {"found": 2, "empty": 1}


def test_large_stats_do_not_materialize_event_rows(tmp_path):
    repo = ScheduleRepository(tmp_path / "test.db")
    repo.initialize()
    now = datetime.now(timezone.utc)
    with repo._connect() as c:
        c.executemany(
            "INSERT INTO usage_events(timestamp,actor_key,chat_type,event_type,properties_json) VALUES (?,?,'private','request',?)",
            (
                (now.isoformat(), f"actor-{i}", '{"scope":"today","result":"found"}')
                for i in range(100000)
            ),
        )
        c.commit()
    gc.collect()
    tracemalloc.start()
    try:
        result = repo.usage_stats(now)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result["active_actors"] == 100000 and result["events_by_type"] == {
        "request": 100000
    }
    assert peak < 4 * 1024 * 1024


@pytest.mark.asyncio
async def test_runtime_uses_configured_storage_and_update_limit(tmp_path, monkeypatch):
    from schedule_bot import config
    from schedule_bot.bot import app

    monkeypatch.setattr(config, "_load_dotenv", lambda: None)
    settings = replace(
        config.Settings.from_env(require_bot_token=False),
        database_path=tmp_path / "test.db",
        backups_path=tmp_path / "backups",
        telegram_max_concurrent_updates=7,
        schedule_stale_after_minutes=17,
    )
    async def polling(*args, **kwargs):
        await asyncio.sleep(0)  # Let the startup pin job run before shutdown.

    dispatcher = SimpleNamespace(include_router=Mock(), start_polling=AsyncMock(side_effect=polling), message=SimpleNamespace(outer_middleware=Mock()))
    factory = Mock(return_value=dispatcher)
    monkeypatch.setattr(app, "Dispatcher", factory)
    monkeypatch.setattr(
        app,
        "Bot",
        Mock(return_value=SimpleNamespace(session=SimpleNamespace(close=AsyncMock()))),
    )
    monkeypatch.setattr(app, "configure_commands", AsyncMock())
    monkeypatch.setattr(app, "create_telegram_session", Mock(return_value=app.Bot.return_value.session))
    monkeypatch.setattr(app, "automatic_update_job", AsyncMock())
    pins = AsyncMock()
    monkeypatch.setattr(app, "sync_schedule_pins", pins)
    monkeypatch.setattr(app.ScheduleNotifier, "run_delivery_worker", AsyncMock())
    await app.run_bot(settings)
    assert isinstance(factory.call_args.kwargs["storage"], ExpiringMemoryStorage)
    assert dispatcher.start_polling.call_args.kwargs["tasks_concurrency_limit"] == 7
    pins.assert_awaited_once()
    assert pins.await_args.kwargs["stale_after_minutes"] == 17
    await factory.call_args.kwargs["storage"].close()


def test_quarantined_digest_never_bypasses_parser(tmp_path, schedule_workbook):
    repo = ScheduleRepository(tmp_path / "test.db")
    good = ExcelScheduleParser().parse(schedule_workbook)
    rejected = replace(good, validation_errors=("Invalid layout",))
    digest = hashlib.sha256(schedule_workbook.read_bytes()).hexdigest()
    with pytest.raises(ImportQuarantinedError):
        repo.import_schedule(rejected, sha256=digest)
    assert repo.find_import(digest) is None
    parser = Mock()
    parser.parse.side_effect = ValueError("Parser still runs for rejected files")
    updater = ScheduleUpdater(None, parser, repo)
    with pytest.raises(ValueError, match="still runs"):
        updater.import_local(schedule_workbook)
    parser.parse.assert_called_once()


def test_cli_and_repository_do_not_load_telegram_framework():
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            "import sys; import schedule_bot.cli; import schedule_bot.repository; assert 'aiogram' not in sys.modules",
        ],
        env=environment,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, -1, 1.5])
async def test_invalid_delivery_budget_is_rejected(tmp_path, limit):
    notifier = ScheduleNotifier(None, ScheduleRepository(tmp_path / "unused.db"), ())
    with pytest.raises(ValueError):
        await notifier.deliver_pending(limit=limit)
