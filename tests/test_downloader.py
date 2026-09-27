from datetime import date

import httpx
import pytest

from schedule_bot.downloader import ScheduleDownloader, resolve_download_url


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
