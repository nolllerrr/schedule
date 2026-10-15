import asyncio
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone

import httpx
import pytest

from schedule_bot import downloader as downloader_module
from schedule_bot.downloader import (
    ScheduleDownloadError,
    ScheduleDownloader,
    resolve_download_url,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("proxy_url", [None, "http://user:secret@proxy.test:8080"])
async def test_schedule_proxy_transport(tmp_path, monkeypatch, proxy_url) -> None:
    options = []

    def transport(**kwargs):
        options.append(kwargs)
        return httpx.MockTransport(lambda request: httpx.Response(200, text=HTML))

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", transport)
    monkeypatch.setenv("HTTPS_PROXY", "http://unrelated.test:8080")
    downloader = ScheduleDownloader(
        "https://example.test/", tmp_path, proxy_url=proxy_url
    )
    link = await downloader.find_latest()
    assert link.filename == "21.-23.09.2026.xlsx"
    assert options == [{
        "local_address": "0.0.0.0", "proxy": proxy_url, "trust_env": False
    }]


def test_schedule_proxy_setting_is_separate_and_hidden(monkeypatch) -> None:
    from schedule_bot import config

    monkeypatch.setattr(config, "_load_dotenv", lambda: None)
    monkeypatch.setenv("SCHEDULE_PROXY_URL", "http://user:secret@proxy.test:8080")
    monkeypatch.setenv("TELEGRAM_PROXY_URL", "")
    settings = config.Settings.from_env(require_bot_token=False)
    assert settings.schedule_proxy_url == "http://user:secret@proxy.test:8080"
    assert settings.telegram_proxy_url is None
    assert "secret@proxy.test" not in repr(settings)
    monkeypatch.delenv("SCHEDULE_PROXY_URL")
    assert config.Settings.from_env(require_bot_token=False).schedule_proxy_url is None


HTML = """
<h2>Расписание занятий и объявления:</h2>
<div class="content">
  <table>
    <tr><td>Корпус 1</td><td>Корпус 2</td></tr>
    <tr>
      <td><a href="/files/21.-23.09.2026.xlsx">21.-23.09.2026</a></td>
      <td><a href="/files/21.09.26-25.09.26.xlsx">21.-25.09.2026</a></td>
    </tr>
  </table>
</div>
"""


def test_downloader_selects_requested_building() -> None:
    first = ScheduleDownloader(
        "https://example.test/schedule/", "downloads", building=1
    ).find_latest_in_html(HTML)
    second = ScheduleDownloader(
        "https://example.test/schedule/", "downloads", building=2
    ).find_latest_in_html(HTML)

    assert first.filename == "21.-23.09.2026.xlsx"
    assert first.end_date == date(2026, 9, 23)
    assert second.filename == "21.09.26-25.09.26.xlsx"
    assert second.end_date == date(2026, 9, 25)


def test_latest_single_day_schedule_is_selected_over_imported_range() -> None:
    html = HTML.replace("21.-23.09.2026.xlsx", "28.09.-02.10.2026.xlsx").replace(
        "</a></td>",
        '</a><a href="/files/05.10.2026.xlsx">05.10.2026г.</a></td>',
        1,
    )
    link = ScheduleDownloader(
        "https://example.test/schedule/", "downloads", building=1
    ).find_latest_in_html(html)
    assert link.filename == "05.10.2026.xlsx"
    assert link.start_date == link.end_date == date(2026, 10, 5)


def test_resolves_nubex_static_proxy_url() -> None:
    proxy_url = (
        "https://ptgh.onego.ru/_/static/r1.nubex.ru/"
        "s1748-17b/f60490_34/21.-24.09.2026.xlsx"
    )

    assert resolve_download_url(proxy_url) == (
        "https://r1.nubex.ru/s1748-17b/f60490_34/21.-24.09.2026.xlsx"
    )


def test_does_not_rewrite_unknown_static_host() -> None:
    url = "https://example.test/_/static/files.example.test/schedule.xlsx"

    assert resolve_download_url(url) == url


@pytest.mark.asyncio
async def test_revalidates_workbook_without_downloading_it_again(
    tmp_path, monkeypatch
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/schedule/":
            return httpx.Response(200, text=HTML)
        if request.headers.get("If-None-Match") == '"version-1"':
            return httpx.Response(304)
        return httpx.Response(
            200, content=b"PK workbook", headers={"ETag": '"version-1"'}
        )

    downloader = ScheduleDownloader("https://example.test/schedule/", tmp_path)
    monkeypatch.setattr(
        downloader,
        "_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )

    first = await downloader.fetch_latest()
    second = await downloader.fetch_latest()

    assert second == first
    workbook_requests = [
        request for request in requests if request.url.path.endswith(".xlsx")
    ]
    assert len(workbook_requests) == 2
    assert "If-None-Match" not in workbook_requests[0].headers
    assert workbook_requests[1].headers["If-None-Match"] == '"version-1"'
    assert workbook_requests[1].headers["Cache-Control"] == "no-cache"


@pytest.mark.asyncio
async def test_changed_workbook_gets_downloaded_and_cached(tmp_path, monkeypatch) -> None:
    requests: list[httpx.Request] = []
    workbook_version = 1

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal workbook_version
        requests.append(request)
        if request.url.path == "/schedule/":
            return httpx.Response(200, text=HTML)
        if request.headers.get("If-None-Match") == '"version-1"':
            workbook_version = 2
        return httpx.Response(
            200,
            content=f"PK workbook {workbook_version}".encode(),
            headers={"ETag": f'"version-{workbook_version}"'},
        )

    downloader = ScheduleDownloader("https://example.test/schedule/", tmp_path)
    monkeypatch.setattr(
        downloader,
        "_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )

    first = await downloader.fetch_latest()
    second = await downloader.fetch_latest()

    assert first.sha256 != second.sha256
    assert second.path.read_bytes() == b"PK workbook 2"
    assert requests[-1].headers["If-None-Match"] == '"version-1"'


@pytest.fixture
def retry_delays(monkeypatch) -> list[float]:
    delays = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(downloader_module.asyncio, "sleep", sleep)
    monkeypatch.setattr(downloader_module.random, "uniform", lambda low, high: 1.0)
    return delays


@pytest.mark.asyncio
async def test_get_success_on_first_attempt(tmp_path, retry_delays) -> None:
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, text="ok")

    downloader = ScheduleDownloader("https://example.test/", tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        response = await downloader._get(client, downloader.page_url)

    assert response.text == "ok"
    assert len(requests) == 1
    assert retry_delays == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
async def test_retries_temporary_http_status(tmp_path, retry_delays, status) -> None:
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status if len(requests) == 1 else 200)

    downloader = ScheduleDownloader("https://example.test/", tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        response = await downloader._get(client, downloader.page_url)

    assert response.status_code == 200
    assert len(requests) == 2
    assert retry_delays == [2.0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_type",
    [
        httpx.ConnectTimeout,
        httpx.ReadTimeout,
        httpx.ConnectError,
        httpx.ReadError,
        httpx.WriteError,
        httpx.PoolTimeout,
        httpx.RemoteProtocolError,
    ],
)
async def test_retries_temporary_network_error(tmp_path, retry_delays, error_type) -> None:
    requests = []

    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            raise error_type("temporary failure", request=request)
        return httpx.Response(200)

    downloader = ScheduleDownloader("https://example.test/", tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        response = await downloader._get(client, downloader.page_url)

    assert response.status_code == 200
    assert len(requests) == 2
    assert retry_delays == [2.0]


@pytest.mark.asyncio
async def test_exhausted_attempts_preserve_safe_error_and_cause(
    tmp_path, retry_delays
) -> None:
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(500)

    downloader = ScheduleDownloader("https://example.test/", tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ScheduleDownloadError) as raised:
            await downloader._get(client, downloader.page_url)

    error = raised.value
    assert error.transient is True
    assert error.category == "http_500"
    assert error.detail == "HTTP 500 Internal Server Error"
    assert error.host == "example.test"
    assert error.attempts == 4
    assert isinstance(error.__cause__, httpx.HTTPStatusError)
    assert error.__cause__.response.status_code == 500
    assert len(requests) == 4
    assert retry_delays == [2.0, 4.0, 8.0]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 404, 501])
async def test_permanent_http_status_fails_without_retry(tmp_path, retry_delays, status):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status)

    downloader = ScheduleDownloader("https://example.test/", tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ScheduleDownloadError) as raised:
            await downloader._get(client, downloader.page_url)

    assert raised.value.transient is False
    assert raised.value.category == f"http_{status}"
    assert raised.value.attempts == 1
    assert len(requests) == 1
    assert retry_delays == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_type", [httpx.LocalProtocolError, httpx.UnsupportedProtocol, httpx.ProxyError]
)
async def test_permanent_transport_error_fails_without_retry(
    tmp_path, retry_delays, error_type
) -> None:
    requests = []

    def respond(request):
        requests.append(request)
        raise error_type("proxy password=private-secret", request=request)

    downloader = ScheduleDownloader("https://example.test/", tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ScheduleDownloadError) as raised:
            await downloader._get(client, downloader.page_url)

    assert raised.value.transient is False
    assert raised.value.category == error_type.__name__
    assert "private-secret" not in str(raised.value)
    assert len(requests) == 1
    assert retry_delays == []


@pytest.mark.asyncio
@pytest.mark.parametrize("factor", [0.8, 1.2])
async def test_backoff_jitter_and_maximum_delay(
    tmp_path, monkeypatch, retry_delays, factor
) -> None:
    monkeypatch.setattr(downloader_module.random, "uniform", lambda low, high: factor)
    downloader = ScheduleDownloader(
        "https://example.test/", tmp_path, retry_max_delay=3
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(502))
    ) as client:
        with pytest.raises(ScheduleDownloadError):
            await downloader._get(client, downloader.page_url)

    assert retry_delays == [2 * factor, min(3, 3 * factor), min(3, 3 * factor)]
    assert all(0 <= delay <= 3 for delay in retry_delays)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize(
    "retry_after,expected",
    [("7", 7.0), ("999999", 15.0), ("0", 0.0), ("invalid", 2.0), ("-1", 2.0)],
)
async def test_retry_after_seconds_and_invalid_values(
    tmp_path, retry_delays, status, retry_after, expected
) -> None:
    requests = []

    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(status, headers={"Retry-After": retry_after})
        return httpx.Response(200)

    downloader = ScheduleDownloader("https://example.test/", tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await downloader._get(client, downloader.page_url)

    assert retry_delays == [expected]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "retry_after,expected",
    [
        ("Wed, 14 Oct 2026 18:30:12 GMT", 12.0),
        ("Wed, 14 Oct 2026 18:31:00 GMT", 15.0),
        ("Wed, 14 Oct 2026 18:29:00 GMT", 0.0),
    ],
)
async def test_retry_after_http_date_uses_controlled_clock(
    tmp_path, monkeypatch, retry_delays, retry_after, expected
) -> None:
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 14, 18, 30, tzinfo=timezone.utc)

    monkeypatch.setattr(downloader_module, "datetime", FixedDateTime)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            503 if len(requests) == 1 else 200,
            headers={"Retry-After": retry_after},
        )

    downloader = ScheduleDownloader("https://example.test/", tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await downloader._get(client, downloader.page_url)

    assert retry_delays == [expected]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [500, httpx.ConnectTimeout])
async def test_download_error_never_exposes_credentials_or_query(
    tmp_path, retry_delays, failure
) -> None:
    url = "https://alice:password-secret@example.test/private-token?token=query-secret"

    def respond(request):
        if failure == 500:
            return httpx.Response(500)
        raise failure(
            "http://proxy-user:proxy-secret@proxy.test/?token=proxy-token",
            request=request,
        )

    downloader = ScheduleDownloader(url, tmp_path, max_attempts=1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ScheduleDownloadError) as raised:
            await downloader._get(client, url)

    assert raised.value.host == "example.test"
    assert "example.test" in str(raised.value)
    for private in (
        "alice", "password-secret", "private-token", "query-secret", "proxy-secret",
        "proxy-token", "https://", "http://",
    ):
        assert private not in str(raised.value)
        assert private not in raised.value.detail
    assert raised.value.__cause__ is not None


@pytest.mark.asyncio
async def test_cancellation_during_backoff_propagates(tmp_path, monkeypatch) -> None:
    sleeping = asyncio.Event()
    requests = []

    async def sleep(delay):
        sleeping.set()
        await asyncio.Event().wait()

    def respond(request):
        requests.append(request)
        return httpx.Response(502)

    monkeypatch.setattr(downloader_module.asyncio, "sleep", sleep)
    downloader = ScheduleDownloader("https://example.test/", tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        task = asyncio.create_task(downloader._get(client, downloader.page_url))
        await sleeping.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert len(requests) == 1


@pytest.mark.asyncio
async def test_cancellation_during_request_propagates(tmp_path, retry_delays) -> None:
    async def respond(request):
        raise asyncio.CancelledError()

    downloader = ScheduleDownloader("https://example.test/", tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(asyncio.CancelledError):
            await downloader._get(client, downloader.page_url)

    assert retry_delays == []


@pytest.mark.asyncio
async def test_hard_request_deadline_bounds_stalled_transport_and_attempts(
    tmp_path, retry_delays
) -> None:
    requests = []
    canceled = []

    async def respond(request):
        requests.append(request)
        try:
            await asyncio.Event().wait()
        finally:
            canceled.append(request)

    downloader = ScheduleDownloader(
        "https://example.test/", tmp_path, timeout_seconds=0.01, max_attempts=3
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ScheduleDownloadError) as raised:
            await asyncio.wait_for(downloader._get(client, downloader.page_url), 1)

    error = raised.value
    assert error.transient is True
    assert error.category == "ReadTimeout"
    assert error.detail == "ReadTimeout (request deadline exceeded)"
    assert error.attempts == 3
    assert isinstance(error.__cause__, TimeoutError)
    assert len(requests) == len(canceled) == 3
    assert retry_delays == [2.0, 4.0]


@pytest.mark.asyncio
async def test_hard_deadline_failure_retries_then_succeeds(
    tmp_path, monkeypatch, retry_delays
) -> None:
    deadlines = []
    first_cause = TimeoutError("private proxy credential")

    @asynccontextmanager
    async def controlled_timeout(seconds):
        deadlines.append(seconds)
        yield
        if len(deadlines) == 1:
            raise first_cause

    monkeypatch.setattr(downloader_module.asyncio, "timeout", controlled_timeout)
    downloader = ScheduleDownloader("https://example.test/", tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="ok"))
    ) as client:
        response = await downloader._get(client, downloader.page_url)

    assert response.text == "ok"
    assert deadlines == [30.0, 30.0]
    assert retry_delays == [2.0]


@pytest.mark.asyncio
async def test_deadline_error_preserves_cause_without_exposing_its_text(
    tmp_path, monkeypatch, retry_delays
) -> None:
    cause = TimeoutError("https://name:deadline-secret@proxy.test/?token=secret-query")

    @asynccontextmanager
    async def controlled_timeout(seconds):
        yield
        raise cause

    monkeypatch.setattr(downloader_module.asyncio, "timeout", controlled_timeout)
    downloader = ScheduleDownloader("https://example.test/", tmp_path, max_attempts=1)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200))
    ) as client:
        with pytest.raises(ScheduleDownloadError) as raised:
            await downloader._get(client, downloader.page_url)

    assert raised.value.__cause__ is cause
    assert "deadline-secret" not in str(raised.value)
    assert "secret-query" not in str(raised.value)
    assert raised.value.transient is True
    assert retry_delays == []


@pytest.mark.asyncio
async def test_legacy_retries_keyword_counts_total_attempts(tmp_path, retry_delays):
    downloader = ScheduleDownloader("https://example.test/", tmp_path, retries=2)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(500))
    ) as client:
        with pytest.raises(ScheduleDownloadError) as raised:
            await downloader._get(client, downloader.page_url)

    assert raised.value.attempts == downloader.retries == downloader.max_attempts == 2
    assert retry_delays == [2.0]


def test_content_validation_errors_are_not_temporary_outages(tmp_path) -> None:
    downloader = ScheduleDownloader("https://example.test/", tmp_path)
    with pytest.raises(ScheduleDownloadError) as raised:
        downloader.find_latest_in_html("<h2>Unexpected page</h2>")
    assert raised.value.transient is False


@pytest.mark.parametrize(
    "options",
    [
        {"max_attempts": 0},
        {"max_attempts": 11},
        {"max_attempts": 1.5},
        {"timeout_seconds": 0},
        {"timeout_seconds": -1},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": float("inf")},
        {"retry_max_delay": -1},
        {"retry_max_delay": 61},
        {"retry_max_delay": float("nan")},
        {"retry_max_delay": float("inf")},
    ],
)
def test_unbounded_retry_options_are_rejected(tmp_path, options) -> None:
    with pytest.raises(ValueError):
        ScheduleDownloader("https://example.test/", tmp_path, **options)
