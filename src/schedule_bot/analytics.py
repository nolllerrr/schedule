from __future__ import annotations

import asyncio
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from schedule_bot.repository import ScheduleRepository


ActorKind = Literal["user", "chat", "system"]


class UsageAnalytics:
    """Store privacy-conscious product events without raw Telegram identifiers."""

    def __init__(self, repository: ScheduleRepository, secret: str) -> None:
        self.repository = repository
        self._secret = secret.encode("utf-8")

    def actor_key(self, kind: ActorKind, actor_id: int | str) -> str:
        payload = f"{kind}:{actor_id}".encode("utf-8")
        return hmac.new(self._secret, payload, hashlib.sha256).hexdigest()[:32]

    async def track(
        self,
        event_type: str,
        *,
        actor_id: int | str,
        actor_kind: ActorKind = "user",
        chat_type: str,
        properties: dict[str, Any] | None = None,
    ) -> None:
        actor_key = self.actor_key(actor_kind, actor_id)
        event_properties = dict(properties or {})
        event_properties["_actor_kind"] = actor_kind
        await asyncio.to_thread(
            self.repository.record_usage_event,
            actor_key,
            chat_type=chat_type,
            event_type=event_type,
            properties=event_properties,
        )

    async def summary(self, *, days: int = 7) -> dict[str, Any]:
        since = datetime.now(timezone.utc) - timedelta(days=max(1, days))
        return await asyncio.to_thread(self.repository.usage_stats, since)

    async def prune(self, *, retention_days: int = 90) -> int:
        before = datetime.now(timezone.utc) - timedelta(
            days=max(1, retention_days)
        )
        return await asyncio.to_thread(self.repository.prune_usage_events, before)


def format_stats(stats: dict[str, Any], *, days: int) -> str:
    events = dict(stats.get("events_by_type", {}))
    scopes = dict(stats.get("schedule_scopes", {}))
    results = dict(stats.get("schedule_results", {}))
    requests = int(events.get("schedule_requested", 0))
    empty = int(results.get("empty", 0))
    empty_percent = round(empty * 100 / requests, 1) if requests else 0.0
    notifications_sent = int(stats.get("sent_notifications", 0))
    notifications_failed = int(stats.get("failed_notifications", 0))
    delivery_total = notifications_sent + notifications_failed
    delivery_percent = (
        round(notifications_sent * 100 / delivery_total, 1)
        if delivery_total
        else 0.0
    )

    scope_labels = {
        "today": "Сегодня",
        "tomorrow": "Завтра",
        "date": "Выбор даты",
        "week": "Неделя",
        "next": "Следующий учебный день",
    }
    scope_lines = [
        f"{scope_labels.get(name, name)}: {count}"
        for name, count in sorted(scopes.items(), key=lambda item: (-item[1], item[0]))
    ]
    scope_block = "\n".join(scope_lines) if scope_lines else "Запросов пока нет"

    return (
        f"<b>📊 Статистика за {days} дн.</b>\n\n"
        f"Активных пользователей: <b>{int(stats.get('active_actors', 0))}</b>\n"
        f"Личных профилей: {int(stats.get('private_profiles', 0))}\n"
        f"Активных групповых чатов: {int(stats.get('active_chats', 0))}\n"
        f"Просмотров расписания: <b>{requests}</b>\n\n"
        f"{scope_block}\n\n"
        f"Пустых результатов: {empty_percent}%\n"
        f"Доставка уведомлений: {delivery_percent}%\n"
        f"Ожидают повторной отправки: "
        f"{int(stats.get('pending_notifications', 0))}\n"
        f"Не доставлено окончательно: "
        f"{int(stats.get('failed_notifications', 0))}"
    )
