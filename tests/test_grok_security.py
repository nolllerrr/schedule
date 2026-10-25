from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date
import json
import sys
import tracemalloc
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from schedule_bot import reactions
from schedule_bot.bot import groups
from schedule_bot.domain import ImportResult
from schedule_bot.repository import ScheduleRepository
from schedule_grok import generate_grok_reaction
from schedule_grok import reaction as generator
from test_grok_integration import grok_system as grok_system, settings as settings


@pytest.fixture
def secured(grok_system, monkeypatch):
    grok_system.settings = replace(grok_system.settings, grok_reaction_chat_ids=(-1,))
    monkeypatch.setattr(reactions, "POLL_SECONDS", 0.01)
    return grok_system


async def dispatch(s):
    await reactions.dispatch_grok_reactions(s.bot, s.repo, s.settings, s.result)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["disable", "change_group", "deactivate", "newer_import"])
async def test_consent_or_schedule_changes_during_generation_prevent_send(secured, monkeypatch, mutation):
    s = secured
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(reactions, "generate_grok_reaction", slow)
    task = asyncio.create_task(dispatch(s))
    try:
        await asyncio.wait_for(started.wait(), 2)
        if mutation == "disable":
            s.repo.set_chat_notifications(-1, False)
        elif mutation == "change_group":
            s.repo.save_chat_profile(-1, chat_type="group", chat_title="Test", target="B", configured_by=10)
        elif mutation == "deactivate":
            s.repo.deactivate_chat(-1)
        else:
            newer = s.repo.import_schedule(replace(s.result.parsed, source_filename="new.xlsx"), sha256="newer")
            s.repo.enqueue_notifications([{"import_id": newer.import_id, "destination_id": -1,
                "destination_kind": "chat", "chat_type": "group", "chunk_index": 0, "text": "New schedule"}])
        await asyncio.wait_for(task, 2)
        assert cancelled.is_set()
        s.bot.send_message.assert_not_awaited()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_group_change_before_job_starts_does_not_relabel_old_delivery(secured, monkeypatch):
    s = secured
    s.repo.save_chat_profile(-1, chat_type="group", chat_title="Test", target="B", configured_by=10)
    generate = AsyncMock(return_value="wrong comment")
    monkeypatch.setattr(reactions, "generate_grok_reaction", generate)
    await dispatch(s)
    generate.assert_not_awaited()
    s.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_reaction_waits_for_every_chunk_even_during_main_backoff(secured, monkeypatch):
    s = secured
    s.repo.enqueue_notifications([{"import_id": s.result.import_id, "destination_id": -1,
        "destination_kind": "chat", "chat_type": "group", "chunk_index": 1, "text": "Tail"}])
    with s.repo._connect() as connection:
        tail = connection.execute("SELECT id FROM notification_outbox WHERE destination_id=-1 AND chunk_index=1").fetchone()[0]
    s.repo.mark_notification_failed(tail, "rate limited", retry_after=3600)
    generate = AsyncMock(return_value="reaction")
    monkeypatch.setattr(reactions, "generate_grok_reaction", generate)
    task = asyncio.create_task(dispatch(s))
    try:
        await asyncio.sleep(0.06)
        generate.assert_not_awaited()
        s.bot.send_message.assert_not_awaited()
        s.repo.mark_notification_sent(tail)
        await asyncio.wait_for(task, 2)
        s.bot.send_message.assert_awaited_once_with(-1, "reaction", parse_mode=None)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_failed_main_series_never_gets_a_comment(secured, monkeypatch):
    s = secured
    with s.repo._connect() as connection:
        connection.execute("UPDATE notification_outbox SET status='failed' WHERE destination_id=-1")
        connection.commit()
    generate = AsyncMock()
    monkeypatch.setattr(reactions, "generate_grok_reaction", generate)
    await dispatch(s)
    generate.assert_not_awaited()


def test_sql_reads_only_the_allowlisted_chat_from_a_large_private_queue(secured):
    s = secured
    s.repo.enqueue_notifications({"import_id": s.result.import_id, "destination_id": i,
        "destination_kind": "user", "chat_type": "private", "chunk_index": 0,
        "text": "private text " * 80} for i in range(100, 25100))
    tracemalloc.start()
    try:
        rows = reactions._get_import_deliveries(s.repo, s.result.import_id, s.settings)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(rows) == 1 and rows[0]["destination_id"] == -1
    assert "private text" not in rows[0]["text"]
    assert peak < 512 * 1024


@pytest.mark.asyncio
async def test_reaction_text_and_exception_body_are_absent_from_logs(secured, monkeypatch, caplog):
    s = secured
    secret = "SECRET_GENERATED_TEXT"
    monkeypatch.setattr(reactions, "generate_grok_reaction", AsyncMock(return_value=secret))
    with caplog.at_level("INFO"):
        await dispatch(s)
        s.bot.send_message.side_effect = RuntimeError(secret)
        await dispatch(s)
    assert secret not in caplog.text
    assert "hash=" in caplog.text and "category=RuntimeError" in caplog.text


@pytest.mark.asyncio
async def test_failed_generation_is_not_retried_for_an_identical_chat_text(grok_system, monkeypatch):
    s = grok_system
    generate = AsyncMock(return_value=None)
    monkeypatch.setattr(reactions, "generate_grok_reaction", generate)
    await dispatch(s)
    generate.assert_awaited_once()
    s.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_disable_acknowledgement_cannot_overtake_an_inflight_comment(secured, monkeypatch):
    s = secured
    sending, release, checked = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def send(*args, **kwargs):
        sending.set()
        await release.wait()

    async def admin(*args):
        checked.set()
        return True

    monkeypatch.setattr(reactions, "generate_grok_reaction", AsyncMock(return_value="reaction"))
    monkeypatch.setattr(groups, "_is_chat_admin", admin)
    s.bot.send_message.side_effect = send
    callback = SimpleNamespace(data="gnav:10:toggle_notifications", from_user=SimpleNamespace(id=10),
        message=SimpleNamespace(chat=SimpleNamespace(id=-1, type="group"), edit_text=AsyncMock()), answer=AsyncMock())
    comment = asyncio.create_task(dispatch(s))
    toggle = None
    try:
        await asyncio.wait_for(sending.wait(), 2)
        toggle = asyncio.create_task(groups.group_menu_action(callback, s.bot, s.repo, s.settings, SimpleNamespace(track=AsyncMock())))
        await asyncio.wait_for(checked.wait(), 2)
        await asyncio.sleep(0.03)
        callback.answer.assert_not_awaited()
        assert s.repo.get_chat_profile(-1)["notifications"]
        release.set()
        await asyncio.wait_for(asyncio.gather(comment, toggle), 2)
        assert not s.repo.get_chat_profile(-1)["notifications"]
        callback.answer.assert_awaited_once()
    finally:
        release.set()
        tasks = [task for task in (comment, toggle) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_worker_bounds_100_jobs_and_deduplicates_without_parallel_dispatch(secured, monkeypatch):
    s = secured
    worker = reactions.GrokReactionWorker(s.bot, s.repo, s.settings)
    counts = {"active": 0, "peak": 0, "calls": 0}

    async def fake(*args):
        counts["active"] += 1
        counts["peak"] = max(counts["peak"], counts["active"])
        try:
            await asyncio.sleep(0.01)
            counts["calls"] += 1
        finally:
            counts["active"] -= 1

    monkeypatch.setattr(reactions, "_dispatch_import", fake)
    for identifier in range(1, 101):
        result = ImportResult(identifier, False, (), None, new_dates=(date(2026, 10, 20),))
        worker.enqueue(result)
        worker.enqueue(result)
    assert worker.queue.qsize() == reactions.MAX_PENDING_JOBS
    task = asyncio.create_task(worker.run())
    try:
        await asyncio.wait_for(worker.queue.join(), 2)
        assert counts == {"active": 0, "peak": 1, "calls": reactions.MAX_PENDING_JOBS}
        worker.enqueue(result)
        assert worker.queue.empty()
        assert reactions.GrokReactionWorker(s.bot, s.repo, s.settings).queue.empty()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_expired_job_is_dropped_without_calling_cli(secured, monkeypatch):
    s = secured
    clock = SimpleNamespace(now=10)
    monkeypatch.setattr(reactions, "monotonic", lambda: clock.now)
    fake = AsyncMock()
    monkeypatch.setattr(reactions, "_dispatch_import", fake)
    worker = reactions.GrokReactionWorker(s.bot, s.repo, s.settings)
    worker.enqueue(s.result)
    clock.now += reactions.JOB_TTL_SECONDS + 1
    task = asyncio.create_task(worker.run())
    try:
        await asyncio.wait_for(worker.queue.join(), 1)
        fake.assert_not_awaited()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_migration_keeps_old_rows_unknown_and_rolls_back_on_failure(tmp_path, monkeypatch, settings):
    path = tmp_path / "legacy.db"
    legacy = ScheduleRepository(path)
    legacy.SCHEMA_VERSION = 9
    legacy.initialize()
    legacy.save_chat_profile(-1, chat_type="group", chat_title="Legacy", target="A", configured_by=10)
    now = legacy._utc_timestamp(None)
    with legacy._connect() as connection:
        connection.execute("INSERT INTO imports(id,source_filename,sha256,imported_at,start_date,end_date) VALUES(1,'old.xlsx','old',?,?,?)", (now, '2026-10-20', '2026-10-20'))
        connection.execute("INSERT INTO notification_outbox(import_id,destination_id,destination_kind,chat_type,chunk_index,text,created_at,next_attempt_at,expires_at,status) VALUES(1,-1,'chat','group',0,'Legacy text',?,?,?,'sent')", (now, now, '2099-01-01T00:00:00+00:00'))
        connection.commit()
    latest = ScheduleRepository(path)
    migrate = latest._migrate_to_v10

    def fail(connection):
        migrate(connection)
        raise RuntimeError("injected")

    monkeypatch.setattr(latest, "_migrate_to_v10", fail)
    with pytest.raises(RuntimeError):
        latest.initialize()
    assert latest.current_schema_version() == 9
    with latest._connect() as connection:
        assert "target_snapshot" not in {row[1] for row in connection.execute("PRAGMA table_info(notification_outbox)")}
    monkeypatch.setattr(latest, "_migrate_to_v10", migrate)
    latest.initialize()
    assert latest.current_schema_version() == ScheduleRepository.SCHEMA_VERSION
    with latest._connect() as connection:
        row = connection.execute("SELECT text,target_snapshot FROM notification_outbox").fetchone()
        assert row["text"] == "Legacy text" and row["target_snapshot"] is None
    assert reactions._get_import_deliveries(latest, 1, replace(settings, grok_reaction_chat_ids=(-1,))) == []


def fake_cli(tmp_path, source):
    script = tmp_path / "fake.py"
    script.write_text(source, encoding="utf-8")
    config = tmp_path / "cli.json"
    config.write_text(json.dumps({"cli_args": [str(script)]}), encoding="utf-8")
    return config


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", ["stdout", "stderr", "both"])
async def test_multimegabyte_cli_output_is_killed_with_bounded_parent_memory(settings, tmp_path, stream):
    fds = [1, 2] if stream == "both" else [1 if stream == "stdout" else 2]
    config = fake_cli(tmp_path, f"import os,threading; threads=[threading.Thread(target=os.write,args=(fd,b'x'*(4*1024*1024))) for fd in {fds!r}]; [t.start() for t in threads]; [t.join() for t in threads]")
    tracemalloc.start()
    try:
        text = await generate_grok_reaction("Schedule", cli_path=sys.executable, config_path=config, enabled=True, timeout=5)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert text is None
    assert peak < 2 * 1024 * 1024


@pytest.mark.asyncio
async def test_cli_does_not_inherit_bot_secrets_and_stderr_is_not_logged(settings, tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("BOT_TOKEN", "SECRET_BOT_TOKEN")
    monkeypatch.setenv("ANALYTICS_SALT", "SECRET_ANALYTICS")
    monkeypatch.setenv("XAI_API_KEY", "expected-test-key")
    config = fake_cli(tmp_path, "import os,json; assert 'BOT_TOKEN' not in os.environ; assert 'ANALYTICS_SALT' not in os.environ; assert os.environ['XAI_API_KEY']=='expected-test-key'; print(json.dumps({'text':'ok'}))")
    assert await generate_grok_reaction("Schedule", cli_path=sys.executable, config_path=config, enabled=True) == "ok"
    config = fake_cli(tmp_path, "import sys; print('SECRET_STDERR',file=sys.stderr); raise SystemExit(3)")
    assert await generate_grok_reaction("Schedule", cli_path=sys.executable, config_path=config, enabled=True) is None
    assert "SECRET_STDERR" not in caplog.text


@pytest.mark.asyncio
async def test_generator_serializes_real_cli_processes_and_cancellation_reaps_them(settings, tmp_path, monkeypatch):
    config = fake_cli(tmp_path, "import time; time.sleep(.03); print('ok')")
    collect = generator._collect_output
    counts = {"active": 0, "peak": 0}

    async def counted(process):
        counts["active"] += 1
        counts["peak"] = max(counts["peak"], counts["active"])
        try:
            return await collect(process)
        finally:
            counts["active"] -= 1

    monkeypatch.setattr(generator, "_collect_output", counted)
    result = await asyncio.gather(*(generate_grok_reaction("Schedule", cli_path=sys.executable, config_path=config, enabled=True) for _ in range(4)))
    assert result == ["ok"] * 4 and counts["peak"] == 1
    config = fake_cli(tmp_path, "import time; time.sleep(20)")
    started = asyncio.Event()
    processes = []
    create = asyncio.create_subprocess_exec

    async def capture(*args, **kwargs):
        process = await create(*args, **kwargs)
        processes.append(process)
        started.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    task = asyncio.create_task(generate_grok_reaction("Schedule", cli_path=sys.executable, config_path=config, enabled=True))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert processes[0].returncode is not None


def test_allowlist_cannot_expand_into_an_unbounded_sql_parameter_list(settings):
    with pytest.raises(ValueError, match="64"):
        replace(settings, grok_reaction_chat_ids=tuple(range(-100, -165, -1)))


@pytest.mark.asyncio
async def test_process_cleanup_does_not_log_os_error_body(monkeypatch, caplog):
    def fail(*args):
        raise OSError("SECRET_CLEANUP_DETAIL")

    process = SimpleNamespace(pid=123, returncode=None, kill=fail,
        stdout=SimpleNamespace(read=AsyncMock(return_value=b"")),
        stderr=SimpleNamespace(read=AsyncMock(return_value=b"")), wait=AsyncMock())
    if generator.os.name == "posix":
        monkeypatch.setattr(generator.os, "killpg", fail)
    await generator._stop_process(process)
    assert "OSError" in caplog.text
    assert "SECRET_CLEANUP_DETAIL" not in caplog.text
