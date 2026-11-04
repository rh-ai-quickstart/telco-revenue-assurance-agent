"""Bounded success-only inference cache with identical-call coalescing."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CacheKey:
    """All dimensions that make a cached prediction safe to reuse."""

    tool_name: str
    subject_id: str
    model_version: str
    feature_snapshot: str


type CacheCompute = Callable[[tuple[CacheKey, ...]], Awaitable[Sequence[object]]]


class BoundedSuccessCache:
    """Cache successful per-subject outputs and coalesce identical missing batches."""

    def __init__(self, *, max_entries: int = 2048, ttl_seconds: float = 30.0) -> None:
        if max_entries < 1 or ttl_seconds <= 0:
            raise ValueError("Cache bounds must be positive.")
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._entries: OrderedDict[CacheKey, tuple[float, object]] = OrderedDict()
        self._inflight: dict[tuple[CacheKey, ...], asyncio.Task[dict[CacheKey, object]]] = {}
        self._lock = asyncio.Lock()

    async def get_or_compute(
        self,
        keys: Sequence[CacheKey],
        compute: CacheCompute,
    ) -> list[object]:
        ordered_keys = tuple(keys)
        if not ordered_keys:
            return []
        if len(set(ordered_keys)) != len(ordered_keys):
            raise ValueError("A cache batch cannot contain duplicate subject keys.")

        results: dict[CacheKey, object] = {}
        async with self._lock:
            self._remove_expired_locked(time.monotonic())
            for key in ordered_keys:
                entry = self._entries.get(key)
                if entry is not None:
                    results[key] = entry[1]
                    self._entries.move_to_end(key)
            missing = tuple(key for key in ordered_keys if key not in results)
            task = self._inflight.get(missing) if missing else None
            if missing and task is None:
                task = asyncio.create_task(self._compute_and_store(missing, compute))
                self._inflight[missing] = task

        if task is not None:
            computed = await asyncio.shield(task)
            results.update(computed)
        return [results[key] for key in ordered_keys]

    async def _compute_and_store(
        self,
        keys: tuple[CacheKey, ...],
        compute: CacheCompute,
    ) -> dict[CacheKey, object]:
        task = asyncio.current_task()
        try:
            values = tuple(await compute(keys))
            if len(values) != len(keys):
                raise ValueError("Prediction result count differs from the cache batch.")
            computed = dict(zip(keys, values, strict=True))
            expiry = time.monotonic() + self._ttl_seconds
            async with self._lock:
                for key, value in computed.items():
                    self._entries[key] = (expiry, value)
                    self._entries.move_to_end(key)
                while len(self._entries) > self._max_entries:
                    self._entries.popitem(last=False)
            return computed
        finally:
            async with self._lock:
                if self._inflight.get(keys) is task:
                    self._inflight.pop(keys, None)

    def _remove_expired_locked(self, now: float) -> None:
        expired = [key for key, (expiry, _) in self._entries.items() if expiry <= now]
        for key in expired:
            self._entries.pop(key, None)
