"""Immutable source snapshots and one atomic selection of the effective schedule."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING
from uuid import uuid4

from schedule_bot.domain import ImportResult, ParsedSchedule
from schedule_bot.import_validation import ImportQuarantinedError, validate_import
from schedule_bot.matching import compare_lessons

if TYPE_CHECKING:
    from schedule_bot.repository import ScheduleRepository


@dataclass(frozen=True, slots=True)
class PublicationInput:
    url: str
    sha256: str
    parsed: ParsedSchedule | None = None


def load_snapshot(repository: ScheduleRepository, connection: sqlite3.Connection, import_id: int) -> ParsedSchedule:
    row = connection.execute("SELECT * FROM imports WHERE id=?", (import_id,)).fetchone()
    lessons = tuple(repository._row_to_lesson(item) for item in connection.execute(
        "SELECT * FROM lessons WHERE import_id=? ORDER BY lesson_date,group_name,position", (import_id,)
    ))
    days = tuple(date.fromisoformat(item[0]) for item in connection.execute(
        "SELECT lesson_date FROM schedule_dates WHERE import_id=? ORDER BY lesson_date", (import_id,)
    ))
    return ParsedSchedule(
        source_filename=row["source_filename"], start_date=date.fromisoformat(row["start_date"]),
        end_date=date.fromisoformat(row["end_date"]), dates=days, lessons=lessons,
        warnings=tuple(json.loads(row["warnings_json"])),
        date_groups={date.fromisoformat(day): tuple(groups) for day, groups in json.loads(row["coverage_json"]).items()},
        confirmed_empty_dates=tuple(date.fromisoformat(day) for day in json.loads(row["empty_dates_json"])),
    )


def store_snapshot(connection: sqlite3.Connection, parsed: ParsedSchedule, digest: str, url: str | None, now: str, *, current: bool = False) -> int:
    cursor = connection.execute(
        "INSERT INTO imports(source_url,source_filename,sha256,imported_at,start_date,end_date,"
        "warnings_json,coverage_json,empty_dates_json) VALUES(?,?,?,?,?,?,?,?,?)",
        (url, parsed.source_filename, digest, now, parsed.start_date.isoformat(), parsed.end_date.isoformat(),
         json.dumps(parsed.warnings, ensure_ascii=False),
         json.dumps({day.isoformat(): groups for day, groups in parsed.date_groups.items()}, ensure_ascii=False),
         json.dumps([day.isoformat() for day in parsed.confirmed_empty_dates])),
    )
    import_id = int(cursor.lastrowid)
    connection.executemany("INSERT INTO schedule_dates VALUES(?,?)", ((import_id, day.isoformat()) for day in parsed.dates))
    connection.executemany(
        "INSERT INTO lessons(import_id,is_current,lesson_date,group_name,position,lesson_label,lesson_number,"
        "start_time,end_time,subject,teacher,room,subgroup,raw_json,segments_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ((import_id, int(current), lesson.lesson_date.isoformat(), lesson.group_name, lesson.position,
          lesson.lesson_label, lesson.lesson_number, lesson.start_time, lesson.end_time, lesson.subject,
          lesson.teacher, lesson.room, lesson.subgroup, json.dumps(lesson.raw_data, ensure_ascii=False),
          json.dumps([part.to_dict() for part in lesson.segments], ensure_ascii=False)) for lesson in parsed.lessons),
    )
    return import_id


def reconcile_publications(repository: ScheduleRepository, publications: list[PublicationInput], *, approval_reason: str | None = None) -> ImportResult:
    if not publications:
        raise ValueError("A source check must contain publications")
    if approval_reason is not None and not approval_reason.strip():
        raise ValueError("Approval requires a non-empty review reason")
    repository.initialize()
    now = datetime.now(timezone.utc).isoformat()
    with repository._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            published = {date.fromisoformat(row[0]) for row in connection.execute("SELECT lesson_date FROM active_schedule_dates")}
            prepared: list[tuple[PublicationInput, int | None, ParsedSchedule]] = []
            for entry in publications:
                known = connection.execute("SELECT id FROM imports WHERE sha256=?", (entry.sha256,)).fetchone()
                if known is not None:
                    parsed = load_snapshot(repository, connection, int(known[0]))
                    prepared.append((entry, int(known[0]), parsed))
                    continue
                if entry.parsed is None:
                    raise ValueError("Unknown publication requires a parsed snapshot")
                parsed = entry.parsed
                key = hashlib.sha256(entry.url.encode()).hexdigest()
                previous = connection.execute("SELECT import_id FROM source_publications WHERE publication_key=?", (key,)).fetchone()
                old = load_snapshot(repository, connection, previous[0]) if previous else None
                old_lessons = list(old.lessons) if old else []
                old_groups = dict(old.date_groups) if old else {}
                for lesson in old_lessons:
                    old_groups.setdefault(lesson.lesson_date, set())
                    old_groups[lesson.lesson_date] = set(old_groups[lesson.lesson_date]) | {lesson.group_name}
                errors, anomalies = validate_import(parsed, old_lessons, {day: set(groups) for day, groups in old_groups.items()}, set(old.dates) if old else set())
                if errors or (anomalies and approval_reason is None):
                    repository._quarantine_import(connection, parsed, entry.sha256, entry.url, errors + anomalies, now)
                    connection.commit()
                    raise ImportQuarantinedError(errors + anomalies)
                prepared.append((entry, None, parsed))
            connection.execute("UPDATE source_publications SET present=0")
            snapshots: dict[int, ParsedSchedule] = {}
            stored_digests: dict[str, int] = {}
            for entry, import_id, parsed in prepared:
                if import_id is None:
                    import_id = stored_digests.get(entry.sha256)
                    if import_id is None:
                        import_id = store_snapshot(connection, parsed, entry.sha256, entry.url, now)
                        stored_digests[entry.sha256] = import_id
                snapshots[import_id] = parsed
                key = hashlib.sha256(entry.url.encode()).hexdigest()
                connection.execute(
                    "INSERT INTO source_publications VALUES(?,?,?,?,?,?,1) ON CONFLICT(publication_key) DO UPDATE SET "
                    "import_id=excluded.import_id,start_date=excluded.start_date,end_date=excluded.end_date,"
                    "last_seen_at=excluded.last_seen_at,present=1",
                    (key, entry.url, import_id, parsed.start_date.isoformat(), parsed.end_date.isoformat(), now),
                )
            candidates: dict[date, list[tuple[int, int, ParsedSchedule]]] = {}
            for import_id, parsed in snapshots.items():
                span = (parsed.end_date - parsed.start_date).days
                for day in parsed.dates:
                    candidates.setdefault(day, []).append((span, import_id, parsed))
            winners: dict[date, tuple[int, ParsedSchedule]] = {}
            for day, choices in candidates.items():
                best_span = min(item[0] for item in choices)
                best = sorted((item for item in choices if item[0] == best_span), key=lambda item: item[1])
                def signature(item: tuple[int, int, ParsedSchedule]) -> tuple[object, ...]:
                    return (tuple(sorted((lesson.group_name, repr(lesson.comparable_data())) for lesson in item[2].lessons if lesson.lesson_date == day)),
                            tuple(sorted(item[2].date_groups.get(day, ()))), day in item[2].confirmed_empty_dates)
                if any(signature(item) != signature(best[0]) for item in best[1:]):
                    reasons = (f"{day}: конфликт публикаций одинакового приоритета",)
                    for entry, _, parsed in prepared:
                        if day in parsed.dates:
                            repository._quarantine_import(connection, parsed, entry.sha256, entry.url, reasons, now)
                    connection.commit()
                    raise ImportQuarantinedError(reasons)
                winners[day] = (best[0][1], best[0][2])
            changed_owners = [day for day, (owner, _) in winners.items() if (
                (row := connection.execute("SELECT source_import_id FROM active_schedule_dates WHERE lesson_date=?", (day.isoformat(),)).fetchone()) is None or row[0] != owner
            )]
            if not changed_owners:
                latest = connection.execute("SELECT MAX(effective_import_id) FROM active_schedule_dates").fetchone()[0]
                connection.commit()
                names = sorted({item[2].source_filename for item in prepared})
                return ImportResult(latest, True, (), None, source_filename="; ".join(names), stored_lesson_count=sum(len(item.lessons) for item in snapshots.values()))
            days = tuple(sorted(winners))
            effective = ParsedSchedule(
                source_filename="; ".join(sorted({item[2].source_filename for item in prepared})),
                start_date=min(days), end_date=max(days), dates=days,
                lessons=tuple(lesson for day, (_, parsed) in sorted(winners.items()) for lesson in parsed.lessons if lesson.lesson_date == day),
                date_groups={day: parsed.date_groups.get(day, ()) for day, (_, parsed) in winners.items()},
                confirmed_empty_dates=tuple(day for day, (_, parsed) in winners.items() if day in parsed.confirmed_empty_dates),
            )
            old_lessons = repository._current_for_dates(connection, days)
            previous_groups: dict[date, set[str]] = {}
            for day in days:
                coverage = connection.execute(
                    "SELECT i.coverage_json FROM active_schedule_dates a JOIN imports i "
                    "ON i.id=a.effective_import_id WHERE a.lesson_date=?", (day.isoformat(),)
                ).fetchone()
                if coverage:
                    previous_groups[day] = set(json.loads(coverage[0]).get(day.isoformat(), ()))
            for lesson in old_lessons:
                previous_groups.setdefault(lesson.lesson_date, set()).add(lesson.group_name)
            errors, anomalies = validate_import(effective, old_lessons, previous_groups, set(days))
            if errors or (anomalies and approval_reason is None):
                digest = "effective-review:" + hashlib.sha256(json.dumps(
                    [(day.isoformat(), owner) for day, (owner, _) in sorted(winners.items())]
                ).encode()).hexdigest()
                repository._quarantine_import(connection, effective, digest, None, errors + anomalies, now)
                connection.commit()
                raise ImportQuarantinedError(errors + anomalies)
            changes = compare_lessons(old_lessons, effective.lessons)
            new_dates = tuple(sorted(set(days) - published))
            connection.executemany("UPDATE lessons SET is_current=0 WHERE is_current=1 AND lesson_date=?", ((day.isoformat(),) for day in days))
            event_id = store_snapshot(connection, effective, "effective:" + uuid4().hex, None, now, current=True)
            if approval_reason is not None:
                connection.execute("UPDATE imports SET approval_reason=? WHERE id=?", (approval_reason.strip(), event_id))
                reviewed_digest = "effective-review:" + hashlib.sha256(json.dumps(
                    [(day.isoformat(), owner) for day, (owner, _) in sorted(winners.items())]
                ).encode()).hexdigest()
                connection.execute("UPDATE quarantined_imports SET status='approved' WHERE sha256=?", (reviewed_digest,))
            connection.executemany(
                "INSERT INTO active_schedule_dates VALUES(?,?,?) ON CONFLICT(lesson_date) DO UPDATE SET "
                "source_import_id=excluded.source_import_id,effective_import_id=excluded.effective_import_id",
                ((day.isoformat(), owner, event_id) for day, (owner, _) in winners.items()),
            )
            repository._enqueue_import_notifications(connection, event_id, changes, new_dates, effective.lessons, now)
            connection.commit()
            return ImportResult(event_id, False, changes, effective, new_dates)
        except Exception:
            connection.rollback()
            raise
