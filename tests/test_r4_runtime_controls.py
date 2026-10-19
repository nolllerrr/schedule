from __future__ import annotations

import asyncio
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from schedule_bot import config
from schedule_bot.analytics import UsageAnalytics
from schedule_bot.async_repository import repository_async
from schedule_bot.bot.app import toggle_notifications
from schedule_bot.diagnostics import safe_update_diagnostic
from schedule_bot.downloader import ScheduleDownloadError
from schedule_bot.message_utils import split_message
from schedule_bot.parser import ScheduleParseError
from schedule_bot.repository import ScheduleRepository


class Rendered(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.stack, self.text = [], []
        self.feed(text)
        self.close()
        assert not self.stack
    def handle_starttag(self, tag, attrs):
        self.stack.append(tag)
    def handle_endtag(self, tag):
        assert self.stack and self.stack.pop() == tag
    def handle_data(self, data):
        self.text.append(data)


@pytest.mark.parametrize("text,limit", [
    ("a" * 3899 + "&amp;" + "b" * 10, 3900),
    ("<b>nested <i>" + "&lt;&amp;&#128512;" * 80 + "</i> tail</b>", 37),
    ('<a href="https://example.test/?x=1&amp;y=2"><b>' + "😀" * 99 + "</b></a>", 19),
    ("<b>first\n\nsecond " + "x" * 120 + "</b>", 31),
])
def test_split_preserves_visible_text_and_balances_each_chunk(text, limit):
    chunks = split_message(text, limit)
    visible = []
    for chunk in chunks:
        rendered = "".join(Rendered(chunk).text)
        assert len(rendered.encode("utf-16-le")) // 2 <= limit
        visible.append(rendered)
    assert "".join(visible) == "".join(Rendered(text).text)
    assert all(not chunk.endswith("&") for chunk in chunks)


def test_split_preserves_formatting_and_exact_paragraph_separators():
    chunks = split_message("<b>" + "x" * 200 + "</b>", 50)
    assert len(chunks) == 4
    assert all(chunk.startswith("<b>") and chunk.endswith("</b>") for chunk in chunks)
    assert "".join("".join(Rendered(chunk).text) for chunk in split_message("a\n\nb\n\n", 2)) == "a\n\nb\n\n"
    with pytest.raises(ValueError):
        split_message("text", 0)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_load_dotenv", lambda: None)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "schedule.db"))
    monkeypatch.setenv("BOT_TOKEN", "legacy-token")
    monkeypatch.delenv("ANALYTICS_SALT", raising=False)
    monkeypatch.delenv("ANALYTICS_PREVIOUS_SALTS", raising=False)
    return config.Settings.from_env()


def test_analytics_key_is_persistent_concurrent_and_independent_of_token(settings, tmp_path):
    assert not list(tmp_path.iterdir())  # Settings inspection has no side effects.
    with ThreadPoolExecutor(max_workers=4) as pool:
        keys = list(pool.map(lambda _: settings.resolve_analytics_salt(), range(8)))
    assert keys == ["legacy-token"] * 8
    rotated = replace(settings, bot_token="new-token")
    assert rotated.resolve_analytics_salt() == "legacy-token"
    assert not list(tmp_path.glob(".analytics-key-*"))


def test_explicit_analytics_key_does_not_write_and_previous_keys_support_erasure(settings, tmp_path):
    explicit = replace(settings, analytics_salt="new-key", analytics_salt_is_explicit=True,
                       analytics_previous_salts=("legacy-token", "legacy-token"))
    assert explicit.resolve_analytics_salt() == "new-key"
    assert not list(tmp_path.iterdir())
    repo = ScheduleRepository(tmp_path / "events.db")
    old = UsageAnalytics(repo, "legacy-token")
    new = UsageAnalytics(repo, "new-key", previous_salts=explicit.analytics_previous_salts)
    assert new.actor_keys("user", 123) == (new.actor_key("user", 123), old.actor_key("user", 123))
    assert "legacy-token" not in repr(explicit)


def test_corrupt_persistent_key_fails_without_disclosing_value(settings):
    path = settings.database_path.with_suffix(".db.analytics-salt")
    path.write_text(" ", encoding="utf-8")
    with pytest.raises(ValueError, match="Persistent analytics key is invalid"):
        settings.resolve_analytics_salt()


@pytest.mark.parametrize("value", ['["old-secret",""]', '"old-secret"', 'invalid-secret'])
def test_previous_analytics_configuration_rejects_invalid_values_safely(settings, monkeypatch, value):
    monkeypatch.setenv("ANALYTICS_PREVIOUS_SALTS", value)
    with pytest.raises(ValueError) as caught:
        config.Settings.from_env()
    assert "ANALYTICS_PREVIOUS_SALTS" in str(caught.value)
    assert "old-secret" not in str(caught.value) and "invalid-secret" not in str(caught.value)


@pytest.mark.parametrize("error,code,stage", [
    (ScheduleDownloadError("https://user:secret@host/?token=secret", category="http_502"), "HTTP_502", "загрузка"),
    (ScheduleDownloadError("secret", category="ReadTimeout"), "SOURCE_TIMEOUT", "загрузка"),
    (ScheduleParseError("private workbook contents secret"), "PARSE_ERROR", "разбор"),
    (sqlite3.OperationalError("private path secret"), "DATABASE_ERROR", "база"),
    (RuntimeError("secret"), "INTERNAL_ERROR", "проверка"),
])
def test_update_diagnostics_are_allowlisted(error, code, stage):
    text = safe_update_diagnostic(error)
    assert code in text and stage in text
    assert "secret" not in text and "http://" not in text and "https://" not in text


def test_update_diagnostic_preserves_safe_stage_and_cause():
    error = RuntimeError("secret")
    error.schedule_stage = "workbook"
    error.__cause__ = ScheduleDownloadError("secret", category="http_403")
    assert "HTTP_403" in safe_update_diagnostic(error)
    assert "загрузка Excel" in safe_update_diagnostic(error)
    assert "secret" not in safe_update_diagnostic(error, stage="secret")


@pytest.mark.asyncio
async def test_real_ui_write_waits_for_sqlite_without_blocking_event_loop(tmp_path):
    repo = ScheduleRepository(tmp_path / "lag.db")
    repo.save_profile(123, role="student", target="TEST")
    repo.BUSY_TIMEOUT_MS = 1000
    with closing(sqlite3.connect(repo.database_path)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        message = SimpleNamespace(from_user=SimpleNamespace(id=123), answer=AsyncMock())
        task = asyncio.create_task(toggle_notifications(message, repo, SimpleNamespace(track=AsyncMock())))
        await asyncio.sleep(0.05)
        assert not task.done()
        writer.rollback()
        await asyncio.wait_for(task, 1)
    assert repo.get_profile(123)["notifications"] is False
    message.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_repository_call_finishes_before_lock_scope_can_end(tmp_path):
    repo = ScheduleRepository(tmp_path / "cancel.db")
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    def operation():
        entered.set()
        assert release.wait(2)
        completed.set()
    task = asyncio.create_task(repository_async(repo).call(operation))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert completed.is_set()
    finally:
        release.set()


@pytest.mark.parametrize("name,value", [
    ("PUBLICATION_LOOKBACK_DAYS", "-1"), ("PUBLICATION_LOOKBACK_DAYS", "367"),
    ("PUBLICATION_LOOKAHEAD_DAYS", "0"), ("PUBLICATION_LOOKAHEAD_DAYS", "367"),
    ("PUBLICATION_MAX_FILES", "0"), ("PUBLICATION_MAX_FILES", "129"),
    ("SCHEDULE_STALE_AFTER_MINUTES", "0"), ("SCHEDULE_STALE_AFTER_MINUTES", "10081"),
    ("OUTBOX_RETENTION_DAYS", "6"), ("OUTBOX_RETENTION_DAYS", "3651"),
    ("DOWNLOADS_RETENTION_DAYS", "6"), ("DOWNLOADS_RETENTION_DAYS", "3651"),
])
def test_operational_budgets_are_validated(settings, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        config.Settings.from_env()


@pytest.mark.asyncio
async def test_repository_facades_share_a_bounded_worker_budget(tmp_path):
    repo = ScheduleRepository(tmp_path / "bounded.db")
    loop = asyncio.get_running_loop()
    # Let the repository semaphore, rather than the CPU-dependent default
    # executor size, limit these operations on small CI runners.
    loop.set_default_executor(ThreadPoolExecutor(max_workers=16))
    entered, release = asyncio.Event(), threading.Event()
    mutex = threading.Lock()
    counts = {"active": 0, "maximum": 0}
    def operation():
        with mutex:
            counts["active"] += 1
            counts["maximum"] = max(counts["maximum"], counts["active"])
            if counts["active"] == 8:
                loop.call_soon_threadsafe(entered.set)
        try:
            assert release.wait(10)
        finally:
            with mutex:
                counts["active"] -= 1
    tasks = [asyncio.create_task(repository_async(repo).call(operation)) for _ in range(16)]
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert counts["maximum"] == 8
    finally:
        release.set()
        await asyncio.gather(*tasks)
    assert counts["maximum"] == 8


def test_telegram_timeout_diagnostic_preserves_operation_stage():
    text = safe_update_diagnostic(TimeoutError("secret"), stage="pin")
    assert "TELEGRAM_TIMEOUT" in text and "закрепление" in text and "secret" not in text
