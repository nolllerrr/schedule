from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import date, datetime, timezone

CATEGORIES = {
    "room": "Аудитория", "teacher": "Преподаватель", "subject": "Предмет",
    "time": "Время занятия", "missing": "Лишняя или отсутствующая пара", "other": "Другое",
}
STATUSES = {"new": "Новое", "in_progress": "В работе", "resolved": "Решено", "rejected": "Не подтверждено"}
CONTEXT_DAYS = 7
DRAFT_HOURS = 24
CLOSED_DAYS = 90
DAILY_LIMIT = 3
CHOICE_PAGE_SIZE = 12


class ReportError(ValueError):
    """Only controlled, user-visible messages may be passed to this exception."""


def timestamp(now: datetime | None = None) -> datetime:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def normalize_description(text: str) -> str:
    text = " ".join(unicodedata.normalize("NFC", text).split())
    if text.startswith("/") or not 5 <= len(text) <= 1000:
        raise ReportError("Введите обычный текст от 5 до 1000 символов. Команды и вложения не принимаются.")
    return text


def make_snapshot(role, target, day, lessons) -> dict:
    if role not in {"student", "teacher"}:
        raise ReportError("Этот профиль не поддерживается.")
    rows = []
    for lesson in lessons:
        row = lesson.to_dict()
        row.pop("raw_data", None)
        rows.append(row)
    return {"version": 1, "role": role, "target": target, "date": day.isoformat(), "lessons": rows}


def lesson_choices(snapshot: dict) -> list[tuple[str, str]]:
    choices = []
    for index, lesson in enumerate(snapshot["lessons"]):
        segments = lesson.get("segments") or [lesson]
        for part, segment in enumerate(segments):
            label = f'{lesson["group_name"]} · {lesson["lesson_label"]}'
            if segment.get("subgroup"):
                label += f' · подгруппа {segment["subgroup"]}'
            label += f' · {segment["subject"]}'
            if segment.get("teacher"):
                label += f' · {segment["teacher"]}'
            choices.append((f"l{index}s{part}", label))
    return choices


def context_heading(snapshot: dict) -> str:
    kind = "Преподаватель" if snapshot["role"] == "teacher" else "Группа"
    return f'{kind}: {snapshot["target"]}\nДата: {date.fromisoformat(snapshot["date"]):%d.%m.%Y}'


def selection_label(snapshot: dict, key: str) -> str:
    return dict(lesson_choices(snapshot)).get(key, "Проблема со всем расписанием")


def original_schedule(snapshot: dict) -> str:
    rows = []
    for lesson in snapshot["lessons"]:
        for part in lesson.get("segments") or [lesson]:
            rows.append(
                f'{lesson["group_name"]} · {lesson["lesson_label"]} · '
                f'{lesson.get("start_time") or "—"}–{lesson.get("end_time") or "—"}\n'
                f'{part["subject"]}; преподаватель: {part.get("teacher") or "—"}; '
                f'аудитория: {part.get("room") or "—"}; подгруппа: {part.get("subgroup") or "—"}'
            )
    return "\n\n".join(rows) or "Занятий нет."


def report_summary(report: dict) -> str:
    snapshot = json.loads(report["snapshot_json"])
    return (
        f'{context_heading(snapshot)}\nЗанятие: {selection_label(snapshot, report["lesson_key"])}\n'
        f'Категория: {CATEGORIES[report["category"]]}\n\nОписание:\n{report["description"]}'
    )


def admin_notification(report: dict) -> str:
    return (
        f'⚠️ Новое обращение #{report["id"]}\nСтатус: {STATUSES[report["status"]]}\n'
        f'Создано (UTC): {report["created_at"]}\n\n{report_summary(report)}\n\n'
        f'В исходной карточке:\n{original_schedule(json.loads(report["snapshot_json"]))}\n\n'
        f'Отправитель: Telegram ID {report["user_id"]}'
    )


def message_chunks(text: str) -> list[str]:
    # Plain text throughout this subsystem: Telegram must never parse user HTML.
    # Reserve room for astral Unicode characters (Telegram counts UTF-16 units).
    return [text[index:index + 1900] for index in range(0, len(text), 1900)] or [""]
