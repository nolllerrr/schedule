from __future__ import annotations

import asyncio
import hashlib
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from openpyxl import Workbook, load_workbook
from test_publication_regressions import active_and_outbox, subjects

from schedule_bot.bot import app
from schedule_bot.downloader import ScheduleDownloader
from schedule_bot.parser import ExcelScheduleParser, ScheduleParseError
from schedule_bot.parser.excel import WEEKDAYS
from schedule_bot.repository import ScheduleRepository
from schedule_bot.services import ScheduleUpdater


TODAY = date(2026, 11, 20)
MONDAY = date(2026, 11, 23)
MISNAMED = "26.-27.11.2026.xlsx"
CORRECTED = "23.11.2026.xlsx"
LABEL = "23.11.2026г."


def write_workbook(
    path: Path, sheets: list[tuple[str, date, str]], *, subject="Синтетическое занятие"
):
    workbook = Workbook()
    workbook.remove(workbook.active)
    for title, day, state in sheets:
        sheet = workbook.create_sheet(title)
        sheet.sheet_state = state
        sheet["C4"] = "ПД-12"
        sheet["A6"] = WEEKDAYS[day.weekday()].capitalize()
        sheet["B6"] = "1 пара"
        sheet["B7"] = "09.00-10.30"
        sheet["C6"] = subject
        sheet["C8"] = "Преподаватель"
    workbook.save(path)
    workbook.close()
    return path


@pytest.fixture
def incident_workbook(tmp_path):
    return write_workbook(
        tmp_path / MISNAMED,
        [
            ("23 ноября 2026", MONDAY, "visible"),
            *(
                (f"{day} ноября 2026" + " " * (day - 9), date(2026, 11, day), "hidden")
                for day in range(10, 14)
            ),
        ],
    )


class PublicationSource:
    def __init__(self):
        self.links = []
        self.files = {}
        self.not_modified = []

    def request(self, request: httpx.Request) -> httpx.Response:
        filename = request.url.path.removeprefix("/")
        if filename == "":
            anchors = "".join(
                f'<a href="/{name}">{caption}</a>' for name, caption in self.links
            )
            return httpx.Response(
                200, text=f"<table><tr><td>{anchors}</td></tr></table>"
            )
        content = self.files[filename]
        etag = f'"{hashlib.sha256(content).hexdigest()}"'
        if request.headers.get("If-None-Match") == etag:
            self.not_modified.append(filename)
            return httpx.Response(304)
        return httpx.Response(200, content=content, headers={"ETag": etag})


class MockScheduleDownloader(ScheduleDownloader):
    def __init__(self, download_dir, source):
        super().__init__("https://source.test/", download_dir)
        self.source = source

    def _client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.source.request))

    async def find_all(self, *, today=TODAY):
        return await super().find_all(today=today)


@pytest.fixture
def system(tmp_path, monkeypatch):
    source = PublicationSource()
    downloader = MockScheduleDownloader(tmp_path / "downloads", source)
    repository = ScheduleRepository(tmp_path / "schedule.db")
    repository.save_profile(123, role="student", target="ПД-12")
    updater = ScheduleUpdater(downloader, ExcelScheduleParser(), repository)
    notifier = SimpleNamespace(
        notify_import=AsyncMock(), notify_admins_error=AsyncMock()
    )
    analytics = SimpleNamespace(track=AsyncMock())
    lock = asyncio.Lock()
    monkeypatch.setattr(app, "sync_schedule_pins", AsyncMock())

    async def automatic():
        await app.automatic_update_job(updater, notifier, analytics, lock, AsyncMock())

    async def manual():
        message = SimpleNamespace(from_user=SimpleNamespace(id=123), answer=AsyncMock())
        settings = SimpleNamespace(
            admin_telegram_ids=(123,),
            timezone="Europe/Moscow",
            schedule_stale_after_minutes=120,
        )
        await app.manual_update(
            message, None, settings, repository, updater, notifier, lock, analytics
        )
        return message.answer.await_args.args[0]

    return SimpleNamespace(
        source=source,
        repository=repository,
        notifier=notifier,
        automatic=automatic,
        manual=manual,
    )


@pytest.mark.asyncio
async def test_incident_recovers_automatically_and_source_rename_does_not_resend(
    system, incident_workbook
):
    system.source.links = [(MISNAMED, LABEL)]
    system.source.files[MISNAMED] = incident_workbook.read_bytes()
    await system.automatic()

    active, _, deliveries = before = active_and_outbox(system.repository)
    assert [row[0] for row in active] == [MONDAY.isoformat()]
    assert subjects(system.repository, day=MONDAY, group="ПД-12") == [
        "Синтетическое занятие"
    ]
    assert len(deliveries) == 1
    result = system.notifier.notify_import.await_args.args[0]
    assert result.filename == MISNAMED
    assert system.repository.schedule_freshness()["last_error_code"] is None
    system.notifier.notify_admins_error.assert_not_awaited()

    assert await system.manual() == "Новых файлов нет."
    assert system.source.not_modified == [MISNAMED]
    assert active_and_outbox(system.repository) == before

    system.source.links = [(CORRECTED, LABEL)]
    system.source.files[CORRECTED] = incident_workbook.read_bytes()
    await system.automatic()
    assert system.notifier.notify_import.await_args.args[0].skipped
    assert active_and_outbox(system.repository) == before


@pytest.mark.asyncio
async def test_cached_bytes_recheck_new_caption_and_failed_batch_recovers(
    system, incident_workbook, tmp_path
):
    friday_file = write_workbook(
        tmp_path / "friday.xlsx", [("20 ноября 2026", TODAY, "visible")]
    )
    system.source.links = [(CORRECTED, LABEL)]
    system.source.files[CORRECTED] = incident_workbook.read_bytes()
    await system.automatic()
    before = active_and_outbox(system.repository)
    system.notifier.notify_import.reset_mock()

    system.source.links = [
        ("20.11.2026.xlsx", "20.11.2026г."),
        (CORRECTED, "24.11.2026г."),
    ]
    system.source.files["20.11.2026.xlsx"] = friday_file.read_bytes()
    message = await system.manual()
    assert "PARSE_ERROR" in message
    assert "Файл: 23.11.2026.xlsx" in message
    assert "Даты в подписи: 2026-11-24" in message
    assert "2026-11-23" in message and "диапазон в имени" in message
    assert system.source.not_modified == [CORRECTED]
    assert active_and_outbox(system.repository) == before
    await system.automatic()
    system.notifier.notify_import.assert_not_awaited()
    error = system.notifier.notify_admins_error.await_args.args[0]
    assert isinstance(error, ScheduleParseError)
    assert "ScheduleParseError" not in str(error)
    assert "лист" in str(error).lower()
    assert active_and_outbox(system.repository) == before
    assert (
        system.repository.schedule_freshness()["last_error_code"]
        == "ScheduleParseError"
    )

    system.source.links[-1] = (CORRECTED, LABEL)
    replacement = write_workbook(
        tmp_path / "replacement.xlsx",
        [("23 ноября 2026", MONDAY, "visible")],
        subject="Исправленное занятие",
    )
    system.source.files[CORRECTED] = replacement.read_bytes()
    assert "Импорт завершён" in await system.manual()
    assert subjects(system.repository, day=MONDAY, group="ПД-12") == [
        "Исправленное занятие"
    ]
    assert subjects(system.repository, day=TODAY, group="ПД-12") == [
        "Синтетическое занятие"
    ]
    assert system.repository.schedule_freshness()["last_error_code"] is None
    recovered = active_and_outbox(system.repository)
    await system.automatic()
    assert active_and_outbox(system.repository) == recovered


@pytest.mark.parametrize(
    ("sheets", "reason"),
    [
        (
            [("26 ноября 2026", 26, "hidden")],
            "дата подтверждает диапазон в имени файла",
        ),
        (
            [("10 ноября 2026", 10, "visible")],
            "лист вне подписи не является старым скрытым шаблоном",
        ),
        (
            [("25 ноября 2026", 25, "hidden")],
            "лист вне подписи не является старым скрытым шаблоном",
        ),
        ([("23.11.2026", 23, "hidden")], "дата листа повторяется"),
        ([("23 ноября 2026 – 24", 24, "hidden")], "дата не распознана"),
        ([("личная заметка", 10, "hidden")], "дата не распознана"),
    ],
)
def test_conflicting_evidence_rejects_workbook(tmp_path, sheets, reason):
    path = write_workbook(
        tmp_path / MISNAMED,
        [
            ("23 ноября 2026", MONDAY, "visible"),
            *((title, date(2026, 11, day), state) for title, day, state in sheets),
        ],
    )
    downloader = ScheduleDownloader("https://source.test/", tmp_path)
    link = downloader.find_all_in_html(
        f'<table><tr><td><a href="/{MISNAMED}">{LABEL}</a></td></tr></table>'
    )[0]

    with pytest.raises(ScheduleParseError, match=reason) as caught:
        ExcelScheduleParser().parse(path, caption=link.caption)
    assert "личная заметка" not in str(caught.value)


def test_caption_combines_duplicate_links_and_selects_date_outside_filename_window(
    tmp_path,
):
    downloader = ScheduleDownloader("https://source.test/", tmp_path)
    links = downloader.find_all_in_html(
        '<table><tr><td><a href="/05.01.2026.xlsx">23.11.2026г.</a>'
        '<a href="/05.01.2026.xlsx">24.11.2026г.</a>'
        '<a href="/06.01.2026.xlsx">Скачать</a></td></tr></table>'
    )
    assert downloader.select_publications(links, today=TODAY) == [links[0]]
    assert links[0].caption.ranges == (
        (MONDAY, MONDAY),
        (date(2026, 11, 24), date(2026, 11, 24)),
    )
    assert links[0].caption.conflicts_with((links[0].start_date, links[0].end_date))


def test_multiday_caption_requires_a_unique_visible_worksheet_for_every_date(tmp_path):
    path = write_workbook(
        tmp_path / MISNAMED,
        [
            ("23 ноября 2026", MONDAY, "visible"),
            ("24 ноября 2026", date(2026, 11, 24), "hidden"),
        ],
    )
    downloader = ScheduleDownloader("https://source.test/", tmp_path)
    link = downloader.find_all_in_html(
        f'<table><tr><td><a href="/{MISNAMED}">23.–24.11.2026г.</a></td></tr></table>'
    )[0]
    with pytest.raises(ScheduleParseError, match="лист скрыт"):
        ExcelScheduleParser().parse(path, caption=link.caption)

    workbook = load_workbook(path)
    workbook.worksheets[1].sheet_state = "visible"
    workbook.save(path)
    workbook.close()
    parsed = ExcelScheduleParser().parse(path, caption=link.caption)
    assert parsed.dates == (MONDAY, date(2026, 11, 24))
    assert {lesson.lesson_date for lesson in parsed.lessons} == set(parsed.dates)


def test_caption_recovery_checks_the_layout(incident_workbook, tmp_path):
    workbook = load_workbook(incident_workbook)
    workbook.active["A6"] = "Четверг"
    workbook.save(incident_workbook)
    workbook.close()
    downloader = ScheduleDownloader("https://source.test/", tmp_path)
    link = downloader.find_all_in_html(
        f'<table><tr><td><a href="/{MISNAMED}">{LABEL}</a></td></tr></table>'
    )[0]
    with pytest.raises(ScheduleParseError, match="блок дня не найден"):
        ExcelScheduleParser().parse(incident_workbook, caption=link.caption)


def test_unrecognized_dated_caption_is_not_treated_as_a_neutral_label(
    incident_workbook, tmp_path
):
    downloader = ScheduleDownloader("https://source.test/", tmp_path)
    link = downloader.find_all_in_html(
        f'<table><tr><td><a href="/{CORRECTED}">23.11 или 24.11.2026</a></td></tr></table>'
    )[0]
    with pytest.raises(ScheduleParseError, match="Неоднозначные даты"):
        ExcelScheduleParser().parse(
            incident_workbook,
            source_filename=link.filename,
            caption=link.caption,
        )


def test_no_sheets_diagnostic_includes_calendar_and_layout_failures(tmp_path):
    path = tmp_path / "23.11.2026.xlsx"
    workbook = Workbook()
    workbook.active.title = "23 ноября 2026"
    workbook.create_sheet("24 ноября 2026")
    workbook.create_sheet("Название скрыто в диагностике")
    sheet = workbook.create_sheet("23.11.2026")
    sheet["C4"] = "ПД-12"
    workbook.save(path)
    workbook.close()

    with pytest.raises(
        ScheduleParseError, match="No schedule sheets were parsed"
    ) as caught:
        ExcelScheduleParser().parse(path)
    details = str(caught.value)
    assert "Файл: 23.11.2026.xlsx" in details
    assert "2026-11-24" in details
    assert "дата вне диапазона" in details
    assert "группы не найдены" in details
    assert "блок дня не найден" in details
    assert "дата не распознана" in details
    assert "Название скрыто" not in details
