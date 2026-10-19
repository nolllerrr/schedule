"""Bounded thread access to the synchronous SQLite repository from async code."""
from __future__ import annotations

import asyncio
import os
from functools import partial
from typing import Any, Callable
from weakref import WeakKeyDictionary, WeakValueDictionary

from schedule_bot.repository import ScheduleRepository

_limits: WeakKeyDictionary = WeakKeyDictionary()

class AsyncScheduleRepository:
    def __init__(self, repository: ScheduleRepository, *, max_concurrency: int = 8):
        if max_concurrency < 1:
            raise ValueError("Repository concurrency must be positive")
        self.repository = repository
        self.max_concurrency = max_concurrency

    def _semaphore(self) -> asyncio.Semaphore:
        limits = _limits.setdefault(asyncio.get_running_loop(), WeakValueDictionary())
        key = (os.path.normcase(str(self.repository.database_path.resolve())), self.max_concurrency)
        return limits.setdefault(key, asyncio.Semaphore(self.max_concurrency))

    async def call(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        async with self._semaphore():
            task = asyncio.create_task(asyncio.to_thread(partial(function, *args, **kwargs)))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # A running SQLite transaction must finish before the caller's
                # destination/pin lock is released during graceful cancellation.
                await task
                raise

    async def run(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if method.startswith("_"):
            raise ValueError("Only public repository operations are supported")
        return await self.call(getattr(self.repository, method), *args, **kwargs)

    def __getattr__(self, method: str):
        if method.startswith("_"):
            raise AttributeError(method)
        async def invoke(*args: Any, **kwargs: Any) -> Any:
            return await self.run(method, *args, **kwargs)
        return invoke


def repository_async(repository: ScheduleRepository) -> AsyncScheduleRepository:
    return AsyncScheduleRepository(repository)
