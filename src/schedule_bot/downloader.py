from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

from schedule_bot.parser.excel import ScheduleParseError, parse_filename_date_range


class ScheduleDownloadError(RuntimeError):
    """Raised when the schedule page or workbook cannot be downloaded safely."""


@dataclass(frozen=True, slots=True)
class ScheduleLink:
    url: str
    filename: str
    start_date: date
    end_date: date


@dataclass(frozen=True, slots=True)
class DownloadedSchedule:
    link: ScheduleLink
    path: Path
    sha256: str


def resolve_download_url(url: str) -> str:
    """Resolve Nubex's site proxy URL to the underlying static file host."""
    parsed = urlparse(url)
    parts = parsed.path.split("/")
    if len(parts) < 5 or parts[1:3] != ["_", "static"]:
        return url

    static_host = parts[3].lower()
    if static_host != "nubex.ru" and not static_host.endswith(".nubex.ru"):
        return url

    direct_path = "/" + "/".join(parts[4:])
    return urlunparse(("https", static_host, direct_path, "", parsed.query, ""))


class ScheduleDownloader:
    def __init__(
        self,
        page_url: str,
        download_dir: str | Path,
        *,
        building: int = 1,
        timeout_seconds: float = 30.0,
        max_file_size: int = 20 * 1024 * 1024,
        retries: int = 3,
    ) -> None:
        if building < 1:
            raise ValueError("building must be a positive number")
        self.page_url = page_url
        self.download_dir = Path(download_dir)
        self.building = building
        self.timeout_seconds = timeout_seconds
        self.max_file_size = max_file_size
        self.retries = max(1, retries)

    def _client(self) -> httpx.AsyncClient:
        # The schedule host has no usable IPv6 route, which can hang behind a VPN.
        transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
        return httpx.AsyncClient(
            transport=transport,
            follow_redirects=True,
            timeout=self.timeout_seconds,
            headers={"User-Agent": "PTGH-Schedule-Bot/1.1"},
        )

    async def find_latest(self) -> ScheduleLink:
        async with self._client() as client:
            response = await self._get(client, self.page_url)
        return self.find_latest_in_html(response.text)

    def find_latest_in_html(self, html: str) -> ScheduleLink:
        soup = BeautifulSoup(html, "html.parser")
        heading = next(
            (
                item
                for item in soup.find_all(["h1", "h2", "h3"])
                if "расписание занятий" in item.get_text(" ", strip=True).lower()
            ),
            None,
        )
        container = heading.find_next("div", class_="content") if heading else soup
        table = container.find("table") if container else None
        if table is None:
            raise ScheduleDownloadError("Schedule table was not found on the page")

        candidates: list[ScheduleLink] = []
        for row in table.find_all("tr"):
            cells = row.find_all("td", recursive=False)
            if len(cells) < self.building:
                continue
            for anchor in cells[self.building - 1].find_all("a", href=True):
                url = resolve_download_url(urljoin(self.page_url, anchor["href"]))
                filename = unquote(Path(urlparse(url).path).name)
                if not filename.lower().endswith(".xlsx"):
                    continue
                try:
                    start_date, end_date = parse_filename_date_range(filename)
                except ScheduleParseError:
                    continue
                candidates.append(
                    ScheduleLink(
                        url=url,
                        filename=filename,
                        start_date=start_date,
                        end_date=end_date,
                    )
                )
        if not candidates:
            raise ScheduleDownloadError(
                f"No Excel schedule link was found for building {self.building}"
            )
        return max(candidates, key=lambda item: (item.end_date, item.start_date))

    async def download(self, link: ScheduleLink) -> DownloadedSchedule:
        async with self._client() as client:
            response = await self._get(client, link.url)
            content = response.content

        if len(content) > self.max_file_size:
            raise ScheduleDownloadError("Downloaded workbook is larger than the limit")
        if not content.startswith(b"PK"):
            raise ScheduleDownloadError("Downloaded file is not an XLSX workbook")

        digest = hashlib.sha256(content).hexdigest()
        self.download_dir.mkdir(parents=True, exist_ok=True)
        safe_name = Path(link.filename).name
        destination = self.download_dir / f"{digest[:12]}_{safe_name}"
        if not destination.exists():
            destination.write_bytes(content)
        return DownloadedSchedule(link=link, path=destination, sha256=digest)

    async def fetch_latest(self) -> DownloadedSchedule:
        return await self.download(await self.find_latest())

    async def _get(self, client: httpx.AsyncClient, url: str) -> httpx.Response:
        last_error: httpx.HTTPError | None = None
        for attempt in range(self.retries):
            try:
                response = await client.get(url)
                response.raise_for_status()
                return response
            except httpx.HTTPError as error:
                last_error = error
                if attempt + 1 < self.retries:
                    await asyncio.sleep(2**attempt)
        error_detail = str(last_error) or type(last_error).__name__
        raise ScheduleDownloadError(
            f"Could not download {url}: {error_detail}"
        ) from last_error
