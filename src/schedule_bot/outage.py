"""Persistent source incident tracking, separate from import and pin errors."""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import re
from contextlib import AsyncExitStack
from datetime import datetime, timezone as utc_timezone
from typing import Callable
from urllib.parse import urlparse
from weakref import WeakKeyDictionary
from zoneinfo import ZoneInfo

from schedule_bot.downloader import ScheduleDownloadError
from schedule_bot.notifier import ScheduleNotifier, _destination_lock
from schedule_bot.repository import ScheduleRepository


_source_locks: WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[tuple[str, str], asyncio.Lock]
] = WeakKeyDictionary()


class ScheduleOutageTracker:
    def __init__(
        self,
        repository: ScheduleRepository,
        notifier: ScheduleNotifier,
        source_url: str,
        *,
        failure_threshold: int = 3,
        timezone: str = "Europe/Moscow",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(failure_threshold, int) or failure_threshold < 1:
            raise ValueError("failure_threshold must be a positive integer")
        self.repository = repository
        self.notifier = notifier
        self.failure_threshold = failure_threshold
        self.timezone = ZoneInfo(timezone)
        self._clock = clock or (lambda: datetime.now(utc_timezone.utc))
        parsed = urlparse(source_url)
        try:
            host = (parsed.hostname or "").encode("idna").decode("ascii").lower()
        except (ValueError, UnicodeError):
            host = ""
        self.source_host = (
            host if re.fullmatch(r"[a-z0-9.:-]+", host) else "неизвестный источник"
        )
        # The database needs an identity, not a URL containing credentials or query values.
        try:
            port = parsed.port
        except ValueError:
            port = None
        source_identity = json.dumps(
            (
                parsed.scheme.lower(),
                host,
                port,
                parsed.path,
                parsed.params,
                parsed.query,
            )
        )
        self.source_key = hashlib.sha256(source_identity.encode("utf-8")).hexdigest()

    def _source_lock(self) -> asyncio.Lock:
        locks = _source_locks.setdefault(asyncio.get_running_loop(), {})
        key = (
            os.path.normcase(str(self.repository.database_path.resolve())),
            self.source_key,
        )
        return locks.setdefault(key, asyncio.Lock())

    def get_state(self) -> dict[str, object]:
        state = self.repository.source_health(self.source_key)
        if state is not None:
            return state
        return {
            "source_key": self.source_key,
            "source_host": self.source_host,
            "status": "HEALTHY",
            "consecutive_failures": 0,
            "first_failure_at": None,
            "incident_started_at": None,
            "last_failure_at": None,
            "last_success_at": None,
            "last_error_category": None,
            "incident_id": None,
            "warning_sent": False,
        }

    async def record_failure(self, error: ScheduleDownloadError) -> dict[str, object]:
        """Count one failed automatic check, after the downloader exhausted retries."""
        async with self._source_lock():
            cached = await asyncio.to_thread(self.repository.has_imported_schedule)
            category, reason, detail = self._error_description(error)
            state = await asyncio.to_thread(
                self.repository.record_source_failure,
                self.source_key,
                self.source_host,
                failure_threshold=self.failure_threshold,
                error_category=category,
                admin_ids=self.notifier.admin_ids,
                warning_text=lambda state: self._warning_text(
                    state, reason, detail, cached
                ),
                now=self._clock(),
            )
        await self.notifier.notify_admin_pending()
        return state

    async def record_success(self) -> dict[str, object]:
        """A real successful source check closes the current incident, including manual checks."""
        async with self._source_lock():
            recipients = await asyncio.to_thread(
                self.repository.source_warning_recipients, self.source_key
            )
            async with AsyncExitStack() as locks:
                # Wait for each in-flight warning and its durable acknowledgment.
                # Pending warnings are then canceled before another send can start.
                for admin_id in sorted(recipients):
                    await locks.enter_async_context(
                        _destination_lock(self.repository, "user", admin_id)
                    )
                state = await asyncio.to_thread(
                    self.repository.record_source_success,
                    self.source_key,
                    self.source_host,
                    recovery_text=self._recovery_text,
                    now=self._clock(),
                )
        await self.notifier.notify_admin_pending()
        return state

    def _format_time(self, value: object) -> str:
        if value is None:
            return "ещё не было"
        timestamp = datetime.fromisoformat(str(value))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=utc_timezone.utc)
        return timestamp.astimezone(self.timezone).strftime("%d.%m.%Y %H:%M")

    def _warning_text(
        self, state: dict[str, object], reason: str, detail: str, cached: bool
    ) -> str:
        cache_text = (
            "Ранее загруженное расписание сохранено."
            if cached
            else "Расписание ещё не было загружено."
        )
        return (
            "⚠️ Проблема с обновлением расписания\n\n"
            f"Источник: {html.escape(self.source_host)}\n"
            f"Причина: {reason}\nПоследняя ошибка: {detail}\n\n"
            f"Неудачных проверок подряд: {state['consecutive_failures']}\n"
            f"Последняя успешная проверка: {self._format_time(state['last_success_at'])}\n\n"
            f"Бот продолжает работать.\n{cache_text}\n\n"
            "Повторная проверка произойдёт автоматически."
        )

    def _recovery_text(self, state: dict[str, object], recovered_at: datetime) -> str:
        started_at = datetime.fromisoformat(str(state["first_failure_at"]))
        minutes = max(0, int((recovered_at - started_at).total_seconds() // 60))
        hours, minutes = divmod(minutes, 60)
        duration = f"{hours} ч {minutes} мин" if hours else f"{minutes} мин"
        return (
            "✅ Доступ к расписанию восстановлен\n\n"
            f"Источник: {html.escape(self.source_host)}\nСостояние: работает\n"
            f"Продолжительность сбоя: {duration}\n"
            f"Последняя успешная проверка: {self._format_time(recovered_at.isoformat())}\n\n"
            "Источник расписания снова доступен для автоматического обновления."
        )

    @staticmethod
    def _error_description(error: ScheduleDownloadError) -> tuple[str, str, str]:
        # Never format the exception or its cause: they may contain a proxy URL or token.
        category = getattr(error, "category", "download_error")
        status_match = re.fullmatch(r"http_(\d{3})", category)
        if status_match:
            status = int(status_match.group(1))
            names = {
                408: "Request Timeout",
                429: "Too Many Requests",
                500: "Internal Server Error",
                502: "Bad Gateway",
                503: "Service Unavailable",
                504: "Gateway Timeout",
            }
            detail = f"HTTP {status} {names.get(status, '')}".strip()
            reason = (
                "слишком много запросов к источнику"
                if status == 429
                else "сервер временно недоступен"
            )
            return category, reason, detail
        descriptions = {
            "ConnectTimeout": ("источник не отвечает вовремя", "ConnectTimeout"),
            "ReadTimeout": ("источник не отвечает вовремя", "ReadTimeout"),
            "WriteTimeout": ("источник не отвечает вовремя", "WriteTimeout"),
            "PoolTimeout": ("источник не отвечает вовремя", "PoolTimeout"),
            "ConnectError": ("не удаётся подключиться к источнику", "ConnectError"),
            "ReadError": ("временная сетевая ошибка", "ReadError"),
            "WriteError": ("временная сетевая ошибка", "WriteError"),
            "CloseError": ("временная сетевая ошибка", "CloseError"),
            "RemoteProtocolError": (
                "временная ошибка соединения",
                "RemoteProtocolError",
            ),
            "connect_timeout": ("источник не отвечает вовремя", "ConnectTimeout"),
            "read_timeout": ("источник не отвечает вовремя", "ReadTimeout"),
            "timeout": ("источник не отвечает вовремя", "тайм-аут запроса"),
            "connect_error": ("не удаётся подключиться к источнику", "ConnectError"),
            "network_error": ("временная сетевая ошибка", "ошибка сети"),
            "remote_protocol_error": (
                "временная ошибка соединения",
                "ошибка протокола HTTP",
            ),
        }
        reason, detail = descriptions.get(
            category, ("источник временно недоступен", "ошибка загрузки")
        )
        safe_category = category if category in descriptions else "download_error"
        return safe_category, reason, detail
