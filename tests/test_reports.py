from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from schedule_bot.domain import Lesson, LessonSegment
from schedule_bot.repository import ScheduleRepository
from schedule_bot.reports.models import ReportError, make_snapshot


NOW = datetime(2026, 10, 29, 6, tzinfo=timezone.utc)
DAY = NOW.date()


@pytest.fixture
def repo(tmp_path):
    repository = ScheduleRepository(tmp_path / "reports.db")
    repository.initialize()
    return repository


def context(repo, user=100, *, source="private", lessons=None, role="student"):
    if lessons is None:
        lessons = [Lesson(DAY, "ИС-22", 1, "1 пара", 1, "09:00", "10:30", "Предмет", "Учитель", "45")]
    return repo.report_create_context(
        source_kind=source, owner_user_id=user if source == "private" else None,
        source_chat_id=user if source == "private" else -100,
        snapshot=make_snapshot(role, "ИС-22", DAY, lessons), now=NOW,
    )


def ready(repo, user=100, *, token=None, description="Аудитория должна быть 47"):
    token = token or context(repo, user)
    draft = repo.report_open_draft(token, user, now=NOW)
    for action in ("lesson:day", "category:room"):
        draft = repo.report_update_draft(draft["id"], user, draft["revision"], action, now=NOW)
    return repo.report_describe(user, description, now=NOW)


def test_fresh_and_repeated_initialization(repo):
    assert repo.current_schema_version() == 12
    ScheduleRepository(repo.database_path).initialize()
    assert repo.get_profile(100) is None


def test_context_is_immutable_and_copied_to_report(repo):
    token = context(repo)
    draft = ready(repo, token=token)
    report = repo.report_submit(draft["id"], 100, draft["revision"], (900, 901), now=NOW)
    assert report["status"] == "new"
    assert '"room":"45"' in report["snapshot_json"]
    with repo._connect() as connection:
        connection.execute("DELETE FROM report_drafts")
        connection.execute("DELETE FROM report_contexts")
        connection.commit()
    assert repo.report_get(report["id"])["snapshot_json"] == report["snapshot_json"]
    assert len(repo.report_pending(now=NOW)) == 2


def test_private_owner_and_expiry(repo):
    token = context(repo)
    with pytest.raises(ReportError):
        repo.report_open_draft(token, 101, now=NOW)
    with pytest.raises(ReportError):
        repo.report_open_draft(token, 100, now=NOW + timedelta(days=7))
    with repo._connect() as connection:
        row = connection.execute("SELECT * FROM report_contexts").fetchone()
    assert row["token_hash"] != token
    assert token not in str(dict(row))


def test_group_context_is_shared_but_drafts_are_owned(repo):
    token = context(repo, source="group")
    first = repo.report_open_draft(token, 100, now=NOW)
    second = repo.report_open_draft(token, 101, now=NOW)
    assert first["id"] != second["id"]
    with pytest.raises(ReportError):
        repo.report_update_draft(first["id"], 101, 0, "lesson:day", now=NOW)
    assert len("report_" + token) <= 64


def test_assignment_choices_keep_segments_and_teacher_groups(repo):
    lesson = Lesson(DAY, "ИС-22", 0, "Классный час", None, None, None, "Общее", segments=(
        LessonSegment("Один", "Первый", "45", "1"),
        LessonSegment("Два", "Второй", "47", "2"),
    ))
    snapshot = make_snapshot("teacher", "Первый", DAY, [lesson, Lesson(DAY, "ПД-12", 1, "1 пара", 1, None, None, "Право")])
    from schedule_bot.reports.models import lesson_choices
    choices = lesson_choices(snapshot)
    assert len(choices) == 3
    assert len({key for key, _ in choices}) == 3
    assert "ИС-22" in choices[0][1] and "подгруппа" in choices[0][1]
    assert "ПД-12" in choices[-1][1]
    token = context(repo, lessons=[lesson])
    draft = repo.report_open_draft(token, 100, now=NOW)
    with pytest.raises(ReportError):
        repo.report_update_draft(draft["id"], 100, 0, "lesson:l999s0", now=NOW)


@pytest.mark.parametrize("description", ["", "    ", "abcd", "x" * 1001, "/start hello"])
def test_invalid_description(repo, description):
    draft = repo.report_open_draft(context(repo), 100, now=NOW)
    draft = repo.report_update_draft(draft["id"], 100, 0, "lesson:day", now=NOW)
    repo.report_update_draft(draft["id"], 100, 1, "category:room", now=NOW)
    with pytest.raises(ReportError):
        repo.report_describe(100, description, now=NOW)


def test_concurrent_confirmation_is_idempotent(repo):
    draft = ready(repo)
    def submit(_):
        return repo.report_submit(draft["id"], 100, draft["revision"], (900,), now=NOW)["id"]
    with ThreadPoolExecutor(max_workers=5) as pool:
        assert len(set(pool.map(submit, range(5)))) == 1
    assert len(repo.report_pending(now=NOW)) == 1


def test_duplicate_and_sliding_limit(repo):
    token = context(repo)
    first = ready(repo, token=token)
    saved = repo.report_submit(first["id"], 100, first["revision"], (900,), now=NOW)
    duplicate = ready(repo, token=token, description="  Аудитория   должна быть 47 ")
    assert repo.report_submit(duplicate["id"], 100, duplicate["revision"], (900,), now=NOW)["id"] == saved["id"]
    for text in ("Другая ошибка первая", "Другая ошибка вторая"):
        draft = ready(repo, description=text)
        repo.report_submit(draft["id"], 100, draft["revision"], (900,), now=NOW)
    draft = ready(repo, description="Четвёртая ошибка")
    with pytest.raises(ReportError, match="24"):
        repo.report_submit(draft["id"], 100, draft["revision"], (900,), now=NOW)


def test_restart_back_and_cancel(repo):
    draft = ready(repo)
    restarted = ScheduleRepository(repo.database_path)
    restored = restarted.report_active_draft(100, now=NOW)
    assert restored["stage"] == "confirm" and restored["description"] == draft["description"]
    edited = restarted.report_update_draft(draft["id"], 100, draft["revision"], "edit", now=NOW)
    assert edited["stage"] == "description"
    restarted.report_cancel(100)
    assert restarted.report_active_draft(100, now=NOW) is None


def test_no_admins_cannot_submit(repo):
    draft = ready(repo)
    with pytest.raises(ReportError):
        repo.report_submit(draft["id"], 100, draft["revision"], (), now=NOW)


def test_atomic_creation_rolls_back_on_outbox_failure(repo):
    draft = ready(repo)
    with repo._connect() as connection:
        connection.execute("CREATE TRIGGER fail_report_queue BEFORE INSERT ON report_outbox BEGIN SELECT RAISE(ABORT,'test'); END")
        connection.commit()
    import sqlite3
    with pytest.raises(sqlite3.Error):
        repo.report_submit(draft["id"], 100, draft["revision"], (900,), now=NOW)
    with repo._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 0
    assert repo.report_active_draft(100, now=NOW)["stage"] == "confirm"


def test_migrate_v11_preserves_existing_data_and_rolls_back(tmp_path, monkeypatch):
    import schedule_bot.repository as module
    from schedule_bot.maintenance import backup_before_migration
    path = tmp_path / "legacy.db"
    legacy = ScheduleRepository(path)
    legacy.SCHEMA_VERSION = 11
    legacy.save_profile(100, role="teacher", target="Преподаватель")
    latest = ScheduleRepository(path)
    backup = backup_before_migration(latest, tmp_path / "backups", now=NOW)
    assert backup.exists()
    original = module.migrate_reports
    def fail(connection):
        original(connection)
        raise RuntimeError("injected migration failure")
    monkeypatch.setattr(module, "migrate_reports", fail)
    with pytest.raises(RuntimeError):
        latest.initialize()
    assert latest.current_schema_version() == 11
    with latest._connect() as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='report_contexts'").fetchone() is None
    monkeypatch.setattr(module, "migrate_reports", original)
    latest.initialize()
    assert latest.get_profile(100)["target"] == "Преподаватель"
    old_backup = ScheduleRepository(backup)
    assert old_backup.current_schema_version() == 11


def test_concurrent_draft_updates_cannot_skip_stages(repo):
    draft = repo.report_open_draft(context(repo), 100, now=NOW)
    def select(_):
        try:
            repo.report_update_draft(draft["id"], 100, 0, "lesson:day", now=NOW)
            return True
        except ReportError:
            return False
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(select, range(4))) == 1
    assert repo.report_active_draft(100, now=NOW)["revision"] == 1


def test_concurrent_limit_and_normalized_duplicate_cannot_create_excess(repo):
    token = context(repo)
    for index in range(2):
        draft = ready(repo, token=token, description=f"Проблема номер {index}")
        repo.report_submit(draft["id"], 100, draft["revision"], (900,), now=NOW)
    draft = ready(repo, token=token, description="Последнее допустимое обращение")
    with ThreadPoolExecutor(max_workers=8) as pool:
        reports = list(pool.map(lambda _: repo.report_submit(draft["id"], 100, draft["revision"], (900,), now=NOW), range(8)))
    assert len({report["id"] for report in reports}) == 1
    duplicate = ready(repo, token=token, description=" Последнее   допустимое обращение ")
    assert repo.report_submit(duplicate["id"], 100, duplicate["revision"], (900,), now=NOW)["id"] == reports[0]["id"]
    assert repo.report_submit(duplicate["id"], 100, duplicate["revision"], (900,), now=NOW)["id"] == reports[0]["id"]
    with repo._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM reports WHERE user_id=100").fetchone()[0] == 3


@pytest.mark.parametrize("steps", [[], ["lesson:day"], ["lesson:day", "category:room"]])
def test_cancel_at_each_stage_and_expired_draft(repo, steps):
    draft = repo.report_open_draft(context(repo), 100, now=NOW)
    for action in steps:
        draft = repo.report_update_draft(draft["id"], 100, draft["revision"], action, now=NOW)
    assert repo.report_active_draft(100, now=NOW + timedelta(hours=24)) is None
    with pytest.raises(ReportError):
        repo.report_update_draft(draft["id"], 100, draft["revision"], "cancel", now=NOW + timedelta(hours=24))
    assert repo.report_update_draft(draft["id"], 100, draft["revision"], "cancel", now=NOW) is None
    assert repo.report_active_draft(100, now=NOW) is None
