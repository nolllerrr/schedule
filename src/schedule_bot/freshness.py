"""Explain source verification separately from the date of a saved lesson."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from schedule_bot.async_repository import repository_async


def freshness_notice(state: dict[str, object], *, timezone_name: str = "Europe/Moscow", stale_after_minutes: int = 120, now: datetime | None = None, show_success: bool = True) -> str:
    value = state.get("last_accepted_at")
    if not value:
        return "⚠️ Актуальность сохранённого расписания ещё не подтверждена."
    checked = datetime.fromisoformat(str(value))
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    stamp = checked.astimezone(ZoneInfo(timezone_name)).strftime("%d.%m.%Y %H:%M")
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    stale = clock - checked > timedelta(minutes=stale_after_minutes)
    if state.get("last_error_code") or stale:
        return f"⚠️ Сохранённое расписание может быть устаревшим.\nПоследняя успешная проверка: {stamp}."
    return f"ℹ️ Последняя успешная проверка: {stamp}." if show_success else ""


async def with_freshness(text: str, repository, settings=None) -> str:
    state = await repository_async(repository).schedule_freshness()
    notice = freshness_notice(
        state, timezone_name=getattr(settings, "timezone", "Europe/Moscow"),
        stale_after_minutes=getattr(settings, "schedule_stale_after_minutes", 120),
    )
    return text + "\n\n" + notice
