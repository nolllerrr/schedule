from __future__ import annotations

import asyncio
import hashlib
import math
import os
import random
import tempfile
import zlib
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

from schedule_bot.parser.excel import ScheduleParseError, parse_filename_date_range


class ScheduleDownloadError(RuntimeError):
    """Raised when the schedule page or workbook cannot be downloaded safely."""

    def __init__(
        self,
        detail: str,
        *,
        transient: bool = False,
        category: str = "download_error",
        attempts: int = 1,
        host: str | None = None,
    ) -> None:
        self.detail = detail
        self.transient = transient
        self.category = category
        self.attempts = attempts
        self.host = host
        if host is not None:
            detail = f"Could not download from {host}: {detail} (attempts: {attempts})"
        super().__init__(detail)


_RETRYABLE_STATUSES = {408, 429, 500, 502, 503, 504}
_RETRYABLE_NETWORK_ERRORS = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)


def _safe_host(url: str) -> str:
    """Keep only the hostname; paths, credentials and query strings are private."""
    try:
        hostname = urlparse(url).hostname
        return hostname.encode("idna").decode("ascii") if hostname else "unknown host"
    except (ValueError, UnicodeError):
        return "unknown host"


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
        max_page_size: int = 2 * 1024 * 1024,
        retries: int | None = None,
        max_attempts: int = 4,
        retry_max_delay: float = 15.0,
        proxy_url: str | None = None,
    ) -> None:
        if building < 1:
            raise ValueError("building must be a positive number")
        if any(not isinstance(value, int) or value < 1 for value in (max_file_size, max_page_size)):
            raise ValueError("Download size limits must be positive")
        if retries is not None:
            # The old keyword counted total attempts, despite its name.
            max_attempts = max(1, retries)
        if not isinstance(max_attempts, int) or not 1 <= max_attempts <= 10:
            raise ValueError("max_attempts must be between 1 and 10")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        if not math.isfinite(retry_max_delay) or not 0 <= retry_max_delay <= 60:
            raise ValueError("retry_max_delay must be between 0 and 60 seconds")
        self.page_url = page_url
        self.download_dir = Path(download_dir)
        self.building = building
        self.timeout_seconds = timeout_seconds
        self.max_file_size = max_file_size
        self.max_page_size = max_page_size
        self.max_attempts = max_attempts
        self.retries = max_attempts
        self.retry_max_delay = retry_max_delay
        self.proxy_url = proxy_url
        self._cached_schedule: DownloadedSchedule | None = None
        self._cached_etag: str | None = None
        self._cached_last_modified: str | None = None

    def _client(self) -> httpx.AsyncClient:
        # The schedule host has no usable IPv6 route, which can hang behind a VPN.
        transport = httpx.AsyncHTTPTransport(
            local_address="0.0.0.0", proxy=self.proxy_url, trust_env=False
        )
        return httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            follow_redirects=True,
            timeout=self.timeout_seconds,
            headers={"User-Agent": "PTGH-Schedule-Bot/1.1", "Accept-Encoding": "identity"},
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
        cached = self._cached_schedule
        validators: dict[str, str] = {}
        if cached is not None and cached.link.url == link.url and cached.path.exists():
            if self._cached_etag:
                validators["If-None-Match"] = self._cached_etag
            if self._cached_last_modified:
                validators["If-Modified-Since"] = self._cached_last_modified
        if validators:
            # The file host advertises a long cache lifetime. Revalidate it anyway.
            validators["Cache-Control"] = "no-cache"

        self.download_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=self.download_dir, suffix=".part", delete=False) as temporary:
            temporary_path = Path(temporary.name)
        try:
            async with self._client() as client:
                response = await self._get(
                    client, link.url, headers=validators, allow_not_modified=bool(validators),
                    download_to=temporary_path,
                )
                if response.status_code == 304:
                    if cached is not None and cached.path.exists():
                        return cached
                    response = await self._get(client, link.url, download_to=temporary_path)
            with temporary_path.open("rb") as source:
                if source.read(2) != b"PK":
                    raise ScheduleDownloadError("Downloaded file is not an XLSX workbook")
                source.seek(0)
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            safe_name = Path(link.filename).name
            destination = self.download_dir / f"{digest[:12]}_{safe_name}"
            if not destination.exists():
                os.replace(temporary_path, destination)
        finally:
            temporary_path.unlink(missing_ok=True)
        downloaded = DownloadedSchedule(link=link, path=destination, sha256=digest)
        self._cached_schedule = downloaded
        self._cached_etag = response.headers.get("ETag")
        self._cached_last_modified = response.headers.get("Last-Modified")
        return downloaded

    async def fetch_latest(self) -> DownloadedSchedule:
        return await self.download(await self.find_latest())

    async def _get(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        allow_not_modified: bool = False,
        download_to: Path | None = None,
    ) -> httpx.Response:
        for attempt in range(1, self.max_attempts + 1):
            response: httpx.Response | None = None
            try:
                # HTTPX's read timeout resets per chunk. A wall-clock deadline
                # also bounds redirects and continuously trickling responses.
                async with asyncio.timeout(self.timeout_seconds):
                    response = await self._read_response(
                        client, url, headers=headers, allow_not_modified=allow_not_modified,
                        download_to=download_to,
                    )
                return response
            except (httpx.HTTPError, httpx.InvalidURL, TimeoutError) as error:
                if isinstance(error, httpx.HTTPStatusError):
                    response = error.response
                    status = error.response.status_code
                    transient = status in _RETRYABLE_STATUSES
                    category = f"http_{status}"
                    detail = f"HTTP {status} {httpx.codes.get_reason_phrase(status)}".strip()
                elif isinstance(error, TimeoutError):
                    transient = True
                    category = "ReadTimeout"
                    detail = "ReadTimeout (request deadline exceeded)"
                else:
                    transient = isinstance(error, _RETRYABLE_NETWORK_ERRORS)
                    category = type(error).__name__
                    detail = category
                if not transient or attempt == self.max_attempts:
                    raise ScheduleDownloadError(
                        detail,
                        transient=transient,
                        category=category,
                        attempts=attempt,
                        host=_safe_host(url),
                    ) from error
                await asyncio.sleep(self._retry_delay(attempt, response))
        raise AssertionError("HTTP attempt loop ended without a response or error")

    async def _read_response(self, client, url, *, headers, allow_not_modified, download_to):
        request = client.build_request("GET", url, headers=headers)
        limit = self.max_file_size if download_to is not None else self.max_page_size
        for redirects in range(client.max_redirects + 1):
            response = await client.send(request, stream=True, follow_redirects=False)
            try:
                # Close redirect bodies unread. HTTPX automatic redirects read
                # their entire body before returning the final response.
                if response.has_redirect_location:
                    if redirects == client.max_redirects:
                        raise httpx.TooManyRedirects("Too many redirects", request=request)
                    request = response.next_request
                    if request is None:
                        raise httpx.RemoteProtocolError("Missing redirect request", request=response.request)
                    continue
                if not (allow_not_modified and response.status_code == 304):
                    response.raise_for_status()
                    length = response.headers.get("Content-Length", "")
                    if length.isdecimal() and int(length) > limit:
                        raise ScheduleDownloadError("HTTP response exceeds the download size limit")
                    if download_to is None:
                        content = bytearray()
                        async for chunk in self._body_chunks(response, limit):
                            content.extend(chunk)
                    else:
                        with download_to.open("wb") as target:
                            async for chunk in self._body_chunks(response, limit):
                                target.write(chunk)
                        content = b""
                else:
                    content = b""
                # The new response contains decoded bytes, so do not decode it
                # again using the original Content-Encoding header.
                response_headers = {k:v for k,v in response.headers.items()
                                    if k.lower() not in {"content-encoding", "content-length"}}
                return httpx.Response(response.status_code, headers=response_headers,
                                      content=bytes(content), request=response.request)
            finally:
                await response.aclose()
        raise AssertionError("Redirect loop ended unexpectedly")

    async def _body_chunks(self, response: httpx.Response, limit: int):
        total = 0
        if response.is_stream_consumed:
            # Mock transports and already-buffered responses provide decoded bytes.
            async for chunk in response.aiter_bytes(chunk_size=65536):
                total += len(chunk)
                if total > limit:
                    raise ScheduleDownloadError("HTTP response exceeds the download size limit")
                yield chunk
            return
        encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
        if encoding not in {"identity", "gzip", "deflate"}:
            raise httpx.DecodingError("Unsupported HTTP content encoding")
        decoder = zlib.decompressobj(31 if encoding == "gzip" else 15) if encoding != "identity" else None
        try:
            async for chunk in response.aiter_raw(chunk_size=65536):
                pending = chunk
                while pending:
                    if decoder is None:
                        decoded, pending = pending, b""
                    else:
                        decoded = decoder.decompress(pending, min(65536, limit - total + 1))
                        pending = decoder.unconsumed_tail
                        if decoder.eof and decoder.unused_data:
                            if encoding != "gzip":
                                raise httpx.DecodingError("Trailing HTTP compressed data")
                            pending = decoder.unused_data
                            decoder = zlib.decompressobj(31)
                    total += len(decoded)
                    if total > limit:
                        raise ScheduleDownloadError("HTTP response exceeds the download size limit")
                    if decoded:
                        yield decoded
            if decoder is not None and not decoder.eof:
                raise httpx.DecodingError("Incomplete HTTP compressed data")
        except zlib.error as error:
            raise httpx.DecodingError("Invalid HTTP compressed data") from error

    def _retry_delay(self, attempt: int, response: httpx.Response | None) -> float:
        """Wait asynchronously with bounded jitter or a bounded Retry-After."""
        if response is not None and response.status_code in {429, 503}:
            raw_retry_after = response.headers.get("Retry-After", "").strip()
            if raw_retry_after:
                try:
                    if raw_retry_after.isdecimal():
                        delay = float(raw_retry_after)
                    else:
                        retry_at = parsedate_to_datetime(raw_retry_after)
                        if retry_at.tzinfo is None:
                            raise ValueError("Retry-After date must have a timezone")
                        delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
                    return min(self.retry_max_delay, max(0.0, delay))
                except (ValueError, TypeError, OverflowError):
                    pass
        base_delay = min(self.retry_max_delay, 2.0**attempt)
        return min(self.retry_max_delay, base_delay * random.uniform(0.8, 1.2))
