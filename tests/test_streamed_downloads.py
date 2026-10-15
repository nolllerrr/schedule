from __future__ import annotations

import asyncio
import gc
import gzip
import hashlib
import tracemalloc
import zlib
from datetime import date

import httpx
import pytest

from schedule_bot.downloader import (
    ScheduleDownloader,
    ScheduleDownloadError,
    ScheduleLink,
)


class CountingStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks, self.consumed, self.closed = chunks, 0, False

    async def __aiter__(self):
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            self.consumed += len(chunk)
            yield chunk

    async def aclose(self):
        self.closed = True


def link():
    return ScheduleLink(
        "https://source.test/file.xlsx",
        "15.10.2026.xlsx",
        date(2026, 10, 15),
        date(2026, 10, 15),
    )


def downloader(tmp_path, monkeypatch, respond, **options):
    instance = ScheduleDownloader(
        "https://source.test/", tmp_path, retry_max_delay=0, **options
    )
    monkeypatch.setattr(
        instance,
        "_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    return instance


@pytest.mark.asyncio
@pytest.mark.parametrize("page", [False, True])
async def test_oversized_chunked_body_is_closed_near_limit(tmp_path, monkeypatch, page):
    stream = CountingStream((b"PK" + bytes(65534) for _ in range(384)))
    instance = downloader(
        tmp_path,
        monkeypatch,
        lambda r: httpx.Response(200, stream=stream),
        max_file_size=1024 * 1024,
        max_page_size=1024 * 1024,
    )
    with pytest.raises(ScheduleDownloadError, match="size limit"):
        if page:
            await instance.find_latest()
        else:
            await instance.download(link())
    assert stream.closed
    assert stream.consumed <= 1024 * 1024 + 65536
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_declared_oversized_body_is_not_read(tmp_path, monkeypatch):
    stream = CountingStream([b"PK contents"])
    instance = downloader(
        tmp_path,
        monkeypatch,
        lambda r: httpx.Response(
            200, headers={"Content-Length": "9999999"}, stream=stream
        ),
        max_file_size=100,
    )
    with pytest.raises(ScheduleDownloadError):
        await instance.download(link())
    assert stream.closed and stream.consumed == 0
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_redirect_body_is_not_buffered(tmp_path, monkeypatch):
    redirect = CountingStream((bytes(65536) for _ in range(384)))
    final = CountingStream([b"PK final workbook"])

    def respond(request):
        if request.url.path == "/file.xlsx":
            return httpx.Response(
                302, headers={"Location": "/actual.xlsx"}, stream=redirect
            )
        return httpx.Response(200, stream=final)

    instance = downloader(tmp_path, monkeypatch, respond)
    result = await instance.download(link())
    assert result.path.read_bytes() == b"PK final workbook"
    assert redirect.consumed == 0 and redirect.closed and final.closed


@pytest.mark.asyncio
async def test_retry_truncates_partial_file_and_publishes_only_complete_download(
    tmp_path, monkeypatch
):
    streams = [
        CountingStream([b"PK stale", httpx.ReadError("connection lost")]),
        CountingStream([b"PK final"]),
    ]
    calls = []

    def respond(request):
        stream = streams[len(calls)]
        calls.append(request)
        return httpx.Response(200, stream=stream)

    instance = downloader(tmp_path, monkeypatch, respond)
    result = await instance.download(link())
    assert len(calls) == 2 and all(s.closed for s in streams)
    assert result.path.read_bytes() == b"PK final"
    assert result.sha256 == hashlib.sha256(b"PK final").hexdigest()
    assert list(tmp_path.iterdir()) == [result.path]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "encoding,compress", [("gzip", gzip.compress), ("deflate", zlib.compress)]
)
async def test_compressed_http_body_is_decoded_once_and_limited(
    tmp_path, monkeypatch, encoding, compress
):
    content = b"PK workbook"
    stream = CountingStream([compress(content)])
    instance = downloader(
        tmp_path,
        monkeypatch,
        lambda r: httpx.Response(
            200, headers={"Content-Encoding": encoding}, stream=stream
        ),
    )
    result = await instance.download(link())
    assert result.path.read_bytes() == content
    oversized = CountingStream([compress(b"PK" + bytes(2 * 1024 * 1024))])
    monkeypatch.setattr(
        instance,
        "_client",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200, headers={"Content-Encoding": encoding}, stream=oversized
                )
            )
        ),
    )
    instance.max_file_size = 1024 * 1024
    with pytest.raises(ScheduleDownloadError, match="size limit"):
        await instance.download(link())
    assert oversized.closed
    assert instance._cached_schedule == result
    assert list(tmp_path.iterdir()) == [result.path]


@pytest.mark.asyncio
async def test_cancelled_download_removes_partial_file(tmp_path, monkeypatch):
    entered = asyncio.Event()

    class WaitingStream(CountingStream):
        async def __aiter__(self):
            yield b"PK" + bytes(65534)
            entered.set()
            await asyncio.Event().wait()

    stream = WaitingStream([])
    instance = downloader(
        tmp_path, monkeypatch, lambda r: httpx.Response(200, stream=stream)
    )
    task = asyncio.create_task(instance.download(link()))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed and not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_deadline_covers_streaming_body_and_cleans_up(tmp_path, monkeypatch):
    streams = []

    class WaitingStream(CountingStream):
        async def __aiter__(self):
            yield b"PK"
            await asyncio.Event().wait()

    def respond(request):
        stream = WaitingStream([])
        streams.append(stream)
        return httpx.Response(200, stream=stream)

    instance = downloader(
        tmp_path, monkeypatch, respond, timeout_seconds=0.02, max_attempts=2
    )
    with pytest.raises(ScheduleDownloadError) as raised:
        await asyncio.wait_for(instance.download(link()), 2)
    assert raised.value.category == "ReadTimeout" and raised.value.attempts == 2
    assert len(streams) == 2 and all(s.closed for s in streams)
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_gzip_expansion_is_bounded_before_allocating_whole_body(
    tmp_path, monkeypatch
):
    payload = gzip.compress(b"PK" + bytes(24 * 1024 * 1024))
    stream = CountingStream([payload])
    instance = downloader(
        tmp_path,
        monkeypatch,
        lambda r: httpx.Response(
            200, headers={"Content-Encoding": "gzip"}, stream=stream
        ),
        max_file_size=1024 * 1024,
    )
    gc.collect()
    tracemalloc.start()
    try:
        with pytest.raises(ScheduleDownloadError):
            await instance.download(link())
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 4 * 1024 * 1024
    assert stream.closed and not list(tmp_path.iterdir())
