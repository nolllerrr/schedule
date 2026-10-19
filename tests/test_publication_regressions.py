from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from schedule_bot.domain import Lesson, ParsedSchedule
from schedule_bot.downloader import DownloadedSchedule, ScheduleDownloadError, ScheduleLink
from schedule_bot.import_validation import ImportQuarantinedError
from schedule_bot.publications import PublicationInput
from schedule_bot.repository import ScheduleRepository
from schedule_bot.services import ScheduleUpdater


DAY = date(2026, 10, 12)
LATER = DAY + timedelta(days=1)


def schedule(name="schedule.xlsx", *, days=(DAY,), groups=("A",), subject="Original", broad=False, empty=False):
    return ParsedSchedule(
        source_filename=name,
        start_date=min(days),
        end_date=max(days) + timedelta(days=int(broad)),
        dates=tuple(days),
        lessons=() if empty else tuple(
            Lesson(day, group, 1, "1 пара", 1, "09:00", "10:30", f"{subject} {group}")
            for day in days for group in groups
        ),
        date_groups={day: tuple(groups) for day in days},
        confirmed_empty_dates=tuple(days) if empty else (),
    )


def publication(url, digest, parsed=None):
    return PublicationInput(f"https://source.test/{url}", digest, parsed)


def subjects(repo, *, day=DAY, group="A"):
    return [lesson.subject for lesson in repo.lessons_for(role="student", target=group, lesson_date=day)]


def active_and_outbox(repo):
    with closing(sqlite3.connect(repo.database_path)) as connection:
        return (
            connection.execute("SELECT * FROM active_schedule_dates ORDER BY lesson_date").fetchall(),
            connection.execute("SELECT * FROM lessons WHERE is_current=1 ORDER BY lesson_date,group_name,position").fetchall(),
            connection.execute("SELECT * FROM notification_outbox ORDER BY id").fetchall(),
        )


def test_two_new_urls_with_same_sha_share_snapshot_and_one_effective_delivery(tmp_path):
    repo = ScheduleRepository(tmp_path / "same.db")
    repo.save_profile(123, role="student", target="A")
    parsed = schedule()
    result = repo.reconcile_publications([
        publication("one", "same-sha", parsed), publication("two", "same-sha", parsed),
    ])
    assert not result.skipped
    assert subjects(repo) == ["Original A"]
    with closing(sqlite3.connect(repo.database_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM imports WHERE sha256='same-sha'").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(DISTINCT import_id),COUNT(*) FROM source_publications").fetchone() == (1, 2)
        assert connection.execute("SELECT COUNT(*) FROM notification_outbox WHERE import_id=?", (result.import_id,)).fetchone()[0] == 1
    before = active_and_outbox(repo)
    assert repo.reconcile_publications([publication("two", "same-sha"), publication("one", "same-sha")]).skipped
    assert active_and_outbox(repo) == before


def test_known_loser_destructive_fallback_is_quarantined_and_can_be_explicitly_approved(tmp_path):
    repo = ScheduleRepository(tmp_path / "fallback.db")
    wide = schedule("wide.xlsx", broad=True)
    narrow = schedule("narrow.xlsx", groups=("A", "B"))
    repo.reconcile_publications([publication("wide", "wide", wide)])
    repo.reconcile_publications([publication("wide", "wide"), publication("narrow", "narrow", narrow)])
    assert subjects(repo, group="B") == ["Original B"]
    before = active_and_outbox(repo)
    with pytest.raises(ImportQuarantinedError, match="B"):
        repo.reconcile_publications([publication("wide", "wide")])
    assert active_and_outbox(repo) == before
    reason = "Проверено исчезновение группы B из актуальной публикации"
    result = repo.reconcile_publications([publication("wide", "wide")], approval_reason=reason)
    assert not result.skipped
    assert subjects(repo) == ["Original A"] and subjects(repo, group="B") == []
    assert [change.kind for change in result.changes] == ["removed"]
    with closing(sqlite3.connect(repo.database_path)) as connection:
        assert connection.execute("SELECT approval_reason FROM imports WHERE id=?", (result.import_id,)).fetchone()[0] == reason


def test_prior_confirmed_empty_day_keeps_header_coverage_against_new_url(tmp_path):
    repo = ScheduleRepository(tmp_path / "empty.db")
    repo.reconcile_publications([publication("empty", "empty", schedule(groups=("A", "B"), empty=True))])
    before = active_and_outbox(repo)
    with pytest.raises(ImportQuarantinedError, match="B"):
        repo.reconcile_publications([publication("new", "new", schedule(groups=("A",)))])
    assert active_and_outbox(repo) == before
    assert subjects(repo) == []


def test_missing_source_date_retains_previously_active_schedule(tmp_path):
    repo = ScheduleRepository(tmp_path / "retained.db")
    repo.reconcile_publications([
        publication("early", "early", schedule("early.xlsx")),
        publication("late", "late", schedule("late.xlsx", days=(LATER,))),
    ])
    before = active_and_outbox(repo)
    assert repo.reconcile_publications([publication("late", "late")]).skipped
    assert active_and_outbox(repo) == before
    assert subjects(repo) == ["Original A"] and subjects(repo, day=LATER) == ["Original A"]


def test_equal_span_conflict_cannot_replace_current_even_with_approval(tmp_path):
    repo = ScheduleRepository(tmp_path / "conflict.db")
    repo.save_profile(123, role="student", target="A")
    repo.reconcile_publications([publication("one", "one", schedule())])
    before = active_and_outbox(repo)
    entries = [publication("one", "one"), publication("two", "two", schedule(subject="Conflicting"))]
    for reason in (None, "Проверено"):
        with pytest.raises(ImportQuarantinedError, match="конфликт"):
            repo.reconcile_publications(entries, approval_reason=reason)
        assert active_and_outbox(repo) == before


def test_structural_errors_never_bypass_review_with_approval(tmp_path):
    repo = ScheduleRepository(tmp_path / "structural.db")
    repo.reconcile_publications([publication("one", "one", schedule())])
    before = active_and_outbox(repo)
    broken = replace(schedule(subject="Changed"), validation_errors=("Не распознан заголовок",))
    with pytest.raises(ImportQuarantinedError, match="заголовок"):
        repo.reconcile_publications([publication("one", "broken", broken)], approval_reason="Проверено")
    assert active_and_outbox(repo) == before


def test_effective_activation_and_source_snapshots_roll_back_with_outbox_sql_error(tmp_path):
    repo = ScheduleRepository(tmp_path / "atomic.db")
    repo.save_profile(123, role="student", target="A")
    repo.reconcile_publications([publication("one", "original", schedule())])
    before = active_and_outbox(repo)
    with closing(sqlite3.connect(repo.database_path)) as connection:
        sources = connection.execute("SELECT * FROM source_publications").fetchall()
        import_count = connection.execute("SELECT COUNT(*) FROM imports").fetchone()[0]
        connection.execute("CREATE TRIGGER abort_delivery BEFORE INSERT ON notification_outbox BEGIN SELECT RAISE(ABORT,'synthetic outbox failure'); END")
        connection.commit()
    with pytest.raises(sqlite3.DatabaseError, match="synthetic outbox failure"):
        repo.reconcile_publications([publication("one", "changed", schedule(subject="Changed"))])
    assert active_and_outbox(repo) == before
    with closing(sqlite3.connect(repo.database_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == import_count
        assert connection.execute("SELECT * FROM source_publications").fetchall() == sources
        assert connection.execute("SELECT 1 FROM imports WHERE sha256='changed'").fetchone() is None


@pytest.mark.parametrize("reverse", [False, True])
def test_narrower_publication_wins_independently_of_input_order(tmp_path, reverse):
    repo = ScheduleRepository(tmp_path / "narrow.db")
    entries = [publication("wide", "wide", schedule(days=(DAY, LATER))),
               publication("narrow", "narrow", schedule(subject="Correction"))]
    repo.reconcile_publications(list(reversed(entries)) if reverse else entries)
    assert subjects(repo) == ["Correction A"]
    assert subjects(repo, day=LATER) == ["Original A"]


@pytest.mark.parametrize("reverse", [False, True])
def test_correction_to_earlier_publication_is_detected_with_unchanged_later_file(tmp_path, reverse):
    repo = ScheduleRepository(tmp_path / "early.db")
    entries = [publication("early", "early-old", schedule()),
               publication("late", "late", schedule(days=(LATER,)))]
    repo.reconcile_publications(list(reversed(entries)) if reverse else entries)
    updates = [publication("late", "late"), publication("early", "early-new", schedule(subject="Corrected early"))]
    result = repo.reconcile_publications(list(reversed(updates)) if reverse else updates)
    assert subjects(repo) == ["Corrected early A"]
    assert subjects(repo, day=LATER) == ["Original A"]
    assert len(result.changes) == 1 and result.changes[0].lesson_date == DAY
    assert result.changes[0].kind == "modified"


def test_new_historical_loser_does_not_block_safe_effective_schedule(tmp_path):
    repo = ScheduleRepository(tmp_path / "loser.db")
    repo.reconcile_publications([publication("narrow", "narrow", schedule(groups=("A", "B")))])
    result = repo.reconcile_publications([
        publication("historical", "historical", schedule(groups=("A",), broad=True)),
        publication("narrow", "narrow"),
    ])
    assert result.skipped
    assert subjects(repo, group="B") == ["Original B"]


@pytest.mark.asyncio
async def test_real_batch_skips_parser_for_known_snapshots_after_updater_restart(tmp_path):
    repo = ScheduleRepository(tmp_path / "skip.db")
    early = ScheduleLink("https://source.test/early", "12.10.2026.xlsx", DAY, DAY)
    late = ScheduleLink("https://source.test/late", "13.10.2026.xlsx", LATER, LATER)
    paths = {early.url: tmp_path / "early.xlsx", late.url: tmp_path / "late.xlsx"}
    for path in paths.values():
        path.write_bytes(b"synthetic content")
    class BatchSource:
        def __init__(self):
            self.checks = 0
        async def find_all(self):
            self.checks += 1
            return [late, early] if self.checks % 2 else [early, late]
        async def download(self, link):
            return DownloadedSchedule(link, paths[link.url], "same-raw-sha")
    source = BatchSource()
    parsed = {early.filename: schedule(early.filename), late.filename: schedule(late.filename, days=(LATER,))}
    parser = SimpleNamespace(parse=Mock(side_effect=lambda path, *, source_filename: parsed[source_filename]))
    first = await ScheduleUpdater(source, parser, repo).update_once()
    assert not first.skipped and parser.parse.call_count == 2
    never_parse = SimpleNamespace(parse=Mock(side_effect=AssertionError("Known batch must not be reparsed")))
    restarted_repo = ScheduleRepository(repo.database_path)
    second = await ScheduleUpdater(source, never_parse, restarted_repo).update_once()
    assert second.skipped
    never_parse.parse.assert_not_called()
    assert subjects(repo) == ["Original A"] and subjects(repo, day=LATER) == ["Original A"]
    with closing(sqlite3.connect(repo.database_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_publications").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_automatic_batch_timeout_counts_one_source_failure(tmp_path):
    repo = ScheduleRepository(tmp_path / "timeout.db")
    class TimeoutSource:
        async def find_all(self):
            raise TimeoutError("synthetic deadline")
    tracker = SimpleNamespace(record_failure=AsyncMock(), record_success=AsyncMock())
    updater = ScheduleUpdater(TimeoutSource(), SimpleNamespace(parse=Mock()), repo, outage_tracker=tracker)
    with pytest.raises(ScheduleDownloadError) as caught:
        await updater.update_once(automatic=True)
    assert getattr(caught.value, "category", None) == "timeout"
    tracker.record_failure.assert_awaited_once()
    tracker.record_success.assert_not_awaited()


def test_reconciliation_preserves_notification_consent_and_deleted_profile_privacy(tmp_path):
    repo = ScheduleRepository(tmp_path / "privacy.db")
    repo.save_profile(123, role="student", target="A")
    repo.save_profile(456, role="student", target="A", notifications=False)
    repo.reconcile_publications([publication("one", "one", schedule())])
    repo.delete_user_data(123, ("synthetic-user-hmac",))
    repo.reconcile_publications([publication("one", "two", schedule(subject="Changed"))])
    assert repo.get_profile(123) is None
    assert repo.get_profile(456)["notifications"] is False
    with closing(sqlite3.connect(repo.database_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM notification_outbox WHERE destination_id IN (123,456)").fetchone()[0] == 0
