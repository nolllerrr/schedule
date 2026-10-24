from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from datetime import date

import pytest

from schedule_bot import card_renderer
from schedule_bot.card_service import (
    CardBusy, CardService, CardTimeout, card_cache_key, close_card_service, get_card_service,
)
from schedule_bot.domain import Lesson, LessonSegment


DAY = date(2026, 10, 26)


def lesson(**changes) -> Lesson:
    return replace(Lesson(DAY, "ИС-22", 3, "2 пара", 2, "10:40", "12:10",
                          "Математика", "Иванов И.И.", "44"), **changes)


def request(service, lessons=None, **changes):
    return service.get_card(role="student", target="ИС-22", lesson_date=DAY,
                            lessons=[lesson()] if lessons is None else lessons, **changes)


@pytest.mark.parametrize("changes", [
    {"position": 4}, {"lesson_label": "Вторая пара"}, {"lesson_number": 3},
    {"start_time": "10:45"}, {"end_time": None}, {"subject": "Русский язык"},
    {"teacher": "Петров П.П."}, {"room": "45"}, {"subgroup": "2"},
    {"group_name": "ИС-23"}, {"lesson_date": date(2026, 10, 27)},
    {"segments": (LessonSegment("Математика", "Иванов И.И.", "44", "1"),)},
])
def test_every_significant_lesson_detail_invalidates_cache(changes):
    profile = dict(role="student", target="ИС-22", lesson_date=DAY)
    assert card_cache_key([lesson()], **profile) != card_cache_key([lesson(**changes)], **profile)


def test_key_is_shared_across_users_and_order_independent(monkeypatch):
    profile = dict(role="student", target="ИС-22", lesson_date=DAY)
    lessons = [lesson(), lesson(lesson_number=4, position=5)]
    original = card_cache_key(lessons, **profile)
    assert card_cache_key(lessons[::-1], **profile) == original
    assert card_cache_key([replace(item, raw_data={"cell": "A1"}) for item in lessons],
                          **profile) == original
    monkeypatch.setattr(card_renderer, "TEMPLATE_VERSION", "next-design")
    assert card_cache_key(lessons, **profile) != original


def test_class_hour_detection_cannot_reuse_a_numbered_pair_cache_entry():
    profile = dict(role="student", target="ИС-22", lesson_date=DAY)
    segmented = lesson(segments=(LessonSegment("Математика", "Иванов И.И.", "44"),))
    assert card_cache_key([segmented], **profile) != card_cache_key(
        [replace(segmented, subject="Классный час")], **profile,
    )


@pytest.mark.asyncio
async def test_lru_reuses_bytes_and_evicts_by_count_and_total_size():
    calls = []

    def render(lessons, **profile):
        calls.append(lessons[0].subject)
        return lessons[0].subject.encode("ascii")

    service = CardService(max_entries=2, max_bytes=5, renderer=render)
    a, b, c = [lesson(subject=subject) for subject in ("aa", "bb", "ccc")]
    assert await request(service, [a]) == b"aa"
    assert await request(service, [b]) == b"bb"
    assert await request(service, [a]) == b"aa"  # a is most recently used
    await request(service, [c])
    assert calls == ["aa", "bb", "ccc"]
    assert (service.cache_entries, service.cache_bytes) == (2, 5)
    await request(service, [b])
    assert calls[-1] == "bb"
    assert service.cache_bytes <= 5
    await service.aclose()
    assert (service.cache_entries, service.cache_bytes) == (0, 0)


@pytest.mark.asyncio
async def test_oversized_result_is_not_retained():
    calls = []

    def render(*args, **kwargs):
        calls.append(1)
        return b"123456"

    service = CardService(max_bytes=5, renderer=render)
    assert await request(service) == b"123456"
    assert await request(service) == b"123456"
    assert len(calls) == 2 and service.cache_bytes == 0


@pytest.mark.asyncio
async def test_single_flight_and_rejected_bursts_leave_event_loop_responsive():
    started, release = threading.Event(), threading.Event()
    calls = []

    def render(*args, **kwargs):
        calls.append(threading.get_ident())
        started.set()
        assert release.wait(3)
        return b"png"

    service = CardService(max_concurrency=1, max_waiters=2, renderer=render)
    first = asyncio.create_task(request(service))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        second = asyncio.create_task(request(service))
        await asyncio.sleep(0)
        with pytest.raises(CardBusy):
            await request(service)  # bounded same-key waiters
        for index in range(20):
            with pytest.raises(CardBusy):
                await request(service, [lesson(subject=str(index))])
        assert service.active_renders == 1
        assert calls == [calls[0]] and calls[0] != threading.get_ident()
        release.set()
        assert await first == await second == b"png"
    finally:
        release.set()
        await service.aclose()


@pytest.mark.asyncio
async def test_timeout_and_cancellation_hold_capacity_until_thread_finishes():
    started, release = threading.Event(), threading.Event()

    def render(*args, **kwargs):
        started.set()
        assert release.wait(3)
        return b"late png"

    service = CardService(max_concurrency=1, timeout=0.03, renderer=render)
    try:
        with pytest.raises(CardTimeout):
            await request(service)
        assert started.is_set() and service.active_renders == 1
        with pytest.raises(CardBusy):
            await request(service, [lesson(subject="another")])
        waiter = asyncio.create_task(request(service))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert service.active_renders == 1
        release.set()
        # Closing waits for the real thread, including after all callers left.
        await service.aclose()
        assert service.active_renders == 0
        with pytest.raises(CardBusy):
            await request(service)
    finally:
        release.set()
        await service.aclose()


@pytest.mark.asyncio
async def test_failure_releases_capacity_and_allows_retry():
    calls = []

    def render(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("render failed")
        return b"recovered"

    service = CardService(renderer=render)
    with pytest.raises(OSError):
        await request(service)
    assert service.active_renders == 0
    assert await request(service) == b"recovered"


@pytest.mark.asyncio
async def test_runtime_service_is_shared_and_closes():
    service = get_card_service()
    assert get_card_service() is service
    await close_card_service()
    assert get_card_service() is not service
    await close_card_service()


@pytest.mark.parametrize("lessons", [[], [lesson(subject="x" * 1025)], [lesson()] * 25])
@pytest.mark.asyncio
async def test_bad_input_never_starts_renderer(lessons):
    def render(*args, **kwargs):
        pytest.fail("invalid input reached render worker")

    with pytest.raises(card_renderer.CardNotSupported):
        await request(CardService(renderer=render), lessons)
