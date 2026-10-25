"""Bounded, process-local PNG cache; no Telegram or database access."""
from __future__ import annotations

import asyncio
import hashlib
import json
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from weakref import WeakKeyDictionary

from schedule_bot import card_renderer
from schedule_bot.card_renderer import CardNotSupported
from schedule_bot.domain import Lesson


class CardBusy(RuntimeError):
    """All render slots or single-flight waiter slots are occupied."""


class CardTimeout(RuntimeError):
    """Caller deadline expired; the bounded worker still owns its slot."""


def card_cache_key(
    lessons: Sequence[Lesson], *, role: str, target: str, lesson_date: date,
) -> str:
    """Hash actual assignments, never Telegram identities or freshness state.

    Bound inputs before serializing them on the event loop. Raw Excel cells are
    deliberately excluded: only the domain fields consumed by the card matter.
    """
    if role not in {"student", "teacher"} or not 0 < len(lessons) <= 24:
        raise CardNotSupported("Для этих данных доступно текстовое расписание.")

    def field(value: str | None) -> str | None:
        if value is not None and (not isinstance(value, str) or len(value) > 1024):
            raise CardNotSupported("Слишком длинные или некорректные данные занятия.")
        return value

    field(target)
    rows = []
    for lesson in lessons:
        if len(lesson.segments) > 4:
            raise CardNotSupported("Слишком много подгрупп для одной карточки.")
        if type(lesson.position) is not int or not -100 <= lesson.position <= 100:
            raise CardNotSupported("Некорректная позиция занятия.")
        if lesson.lesson_number is not None and (
            type(lesson.lesson_number) is not int or not -100 <= lesson.lesson_number <= 100
        ):
            raise CardNotSupported("Некорректный номер пары.")
        rows.append({
            "date": lesson.lesson_date.isoformat(),
            "group": field(lesson.group_name),
            "position": lesson.position,
            "label": field(lesson.lesson_label),
            "subject": field(lesson.subject),
            "number": lesson.lesson_number,
            "start": field(lesson.start_time),
            "end": field(lesson.end_time),
            "segments": [
                [field(part.subject), field(part.teacher), field(part.room), field(part.subgroup)]
                for part in lesson.effective_segments()
            ],
        })
    canonical_rows = sorted(json.dumps(row, ensure_ascii=True, sort_keys=True) for row in rows)
    payload = json.dumps(
        [card_renderer.TEMPLATE_VERSION, role, target, lesson_date.isoformat(), canonical_rows],
        ensure_ascii=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _render(lessons: Sequence[Lesson], *, role: str, target: str, lesson_date: date) -> bytes:
    if role == "teacher":
        return card_renderer.render_teacher_card(lessons, target=target, lesson_date=lesson_date)
    model = card_renderer.normalize_schedule(
        lessons, role=role, target=target, lesson_date=lesson_date,
    )
    return card_renderer.render_card(model)


@dataclass
class _Flight:
    task: asyncio.Task[bytes]
    waiters: int = 0


class CardService:
    """At most two CPU jobs, no pending job queue, bounded same-key waiters.

    A timeout/cancelled caller does not cancel a running Python thread or release
    its capacity early. The flight remains until that actual work has finished.
    Instances belong to one asyncio event loop.
    """

    def __init__(
        self, *, max_concurrency: int = 2, max_entries: int = 32,
        max_bytes: int = 16 * 1024 * 1024, timeout: float = 8.0,
        max_waiters: int = 8, renderer: Callable[..., bytes] = _render,
    ) -> None:
        if (not 1 <= max_concurrency <= 2 or max_entries < 1 or max_bytes < 1
                or timeout <= 0 or max_waiters < 1):
            raise ValueError("Invalid card resource limits")
        self.max_concurrency = max_concurrency
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.timeout = timeout
        self.max_waiters = max_waiters
        self._renderer = renderer
        self._cache: OrderedDict[str, bytes] = OrderedDict()
        self._cache_bytes = 0
        self._flights: dict[str, _Flight] = {}
        self._closed = False

    @property
    def cache_entries(self) -> int:
        return len(self._cache)

    @property
    def cache_bytes(self) -> int:
        return self._cache_bytes

    @property
    def active_renders(self) -> int:
        return len(self._flights)

    async def get_card(
        self, *, role: str, target: str, lesson_date: date, lessons: Sequence[Lesson],
    ) -> bytes:
        if self._closed:
            raise CardBusy("Card service is closing")
        key = card_cache_key(lessons, role=role, target=target, lesson_date=lesson_date)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached
        flight = self._flights.get(key)
        if flight is None:
            if len(self._flights) >= self.max_concurrency:
                raise CardBusy("Card render capacity reached")
            task = asyncio.create_task(self._generate(
                key, tuple(lessons), role=role, target=target, lesson_date=lesson_date,
            ))
            # A timed-out caller may never await the flight again.
            task.add_done_callback(_consume_exception)
            flight = self._flights[key] = _Flight(task)
        if flight.waiters >= self.max_waiters:
            raise CardBusy("Card waiter capacity reached")
        flight.waiters += 1
        try:
            return await asyncio.wait_for(asyncio.shield(flight.task), timeout=self.timeout)
        except TimeoutError as error:
            raise CardTimeout("Card rendering exceeded its deadline") from error
        finally:
            flight.waiters -= 1

    async def _generate(self, key: str, lessons: Sequence[Lesson], **profile) -> bytes:
        worker = asyncio.create_task(asyncio.to_thread(self._renderer, lessons, **profile))
        try:
            try:
                png = await asyncio.shield(worker)
            except asyncio.CancelledError:
                await worker
                raise
            if not isinstance(png, bytes):
                raise TypeError("Card renderer must return bytes")
            if len(png) <= self.max_bytes:
                self._cache[key] = png
                self._cache_bytes += len(png)
                while len(self._cache) > self.max_entries or self._cache_bytes > self.max_bytes:
                    _, evicted = self._cache.popitem(last=False)
                    self._cache_bytes -= len(evicted)
            return png
        finally:
            self._flights.pop(key, None)

    async def aclose(self) -> None:
        self._closed = True
        await asyncio.gather(*(flight.task for flight in list(self._flights.values())),
                             return_exceptions=True)
        self._cache.clear()
        self._cache_bytes = 0


def _consume_exception(task: asyncio.Task) -> None:
    if not task.cancelled():
        task.exception()


_services: WeakKeyDictionary = WeakKeyDictionary()


def get_card_service() -> CardService:
    loop = asyncio.get_running_loop()
    if loop not in _services:
        _services[loop] = CardService()
    return _services[loop]


async def close_card_service() -> None:
    service = _services.pop(asyncio.get_running_loop(), None)
    if service is not None:
        await service.aclose()
