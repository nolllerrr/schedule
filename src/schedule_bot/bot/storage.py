"""Keep only active setup conversations, with bounded capacity and idle expiry."""

from __future__ import annotations

import asyncio
import math
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from time import monotonic
from typing import Any

from aiogram.exceptions import DataNotDictLikeError
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey


@dataclass(slots=True)
class _Record:
    state: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    expires_at: float = 0.0


class ExpiringMemoryStorage(BaseStorage):
    def __init__(
        self,
        *,
        ttl_seconds: float = 1800,
        max_records: int = 5000,
        clock: Callable[[], float] = monotonic,
    ):
        if not math.isfinite(ttl_seconds) or ttl_seconds <= 0 or max_records < 1:
            raise ValueError("FSM expiry and capacity must be positive")
        self.ttl_seconds, self.max_records, self.clock = ttl_seconds, max_records, clock
        self.storage: OrderedDict[StorageKey, _Record] = OrderedDict()
        self._cleanup: asyncio.TimerHandle | None = None

    def _prune(self):
        now = self.clock()
        while self.storage and next(iter(self.storage.values())).expires_at <= now:
            self.storage.popitem(last=False)

    def _expire(self):
        self._cleanup = None
        self._prune()
        self._schedule()

    def _schedule(self):
        if not self.storage:
            if self._cleanup is not None:
                self._cleanup.cancel()
                self._cleanup = None
        elif self._cleanup is None:
            delay = max(
                0.0, next(iter(self.storage.values())).expires_at - self.clock()
            )
            self._cleanup = asyncio.get_running_loop().call_later(delay, self._expire)

    def _get(self, key: StorageKey) -> _Record | None:
        self._prune()
        record = self.storage.get(key)
        if record is not None:
            record.expires_at = self.clock() + self.ttl_seconds
            self.storage.move_to_end(key)
        self._schedule()
        return record

    def _save(self, key: StorageKey, record: _Record):
        if record.state is None and not record.data:
            self.storage.pop(key, None)
        else:
            record.expires_at = self.clock() + self.ttl_seconds
            self.storage[key] = record
            self.storage.move_to_end(key)
            while len(self.storage) > self.max_records:
                self.storage.popitem(last=False)
        self._schedule()

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        record = self._get(key) or _Record()
        record.state = state.state if isinstance(state, State) else state
        self._save(key, record)

    async def get_state(self, key: StorageKey) -> str | None:
        record = self._get(key)
        return record.state if record else None

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        if not isinstance(data, dict):
            raise DataNotDictLikeError("FSM data must be a dict or dict-like object")
        record = self._get(key) or _Record()
        record.data = data.copy()
        self._save(key, record)

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        record = self._get(key)
        return record.data.copy() if record else {}

    async def close(self) -> None:
        self.storage.clear()
        self._schedule()
