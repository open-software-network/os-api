from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import Protocol

from redis.asyncio import Redis


class RateLimiter(Protocol):
    async def allow(self, principal: str) -> tuple[bool, int]: ...


class LocalFixedWindowLimiter:
    """Single-process development limiter; production uses RedisFixedWindowLimiter."""

    def __init__(self, requests_per_minute: int) -> None:
        self.limit = requests_per_minute
        self._windows: dict[str, tuple[int, int]] = defaultdict(lambda: (0, 0))
        self._lock = asyncio.Lock()

    async def allow(self, principal: str) -> tuple[bool, int]:
        window = int(time.time() // 60)
        async with self._lock:
            stored_window, count = self._windows[principal]
            count = count + 1 if stored_window == window else 1
            self._windows[principal] = (window, count)
        return count <= self.limit, max(0, self.limit - count)


class RedisFixedWindowLimiter:
    def __init__(self, redis: Redis, requests_per_minute: int) -> None:
        self.redis = redis
        self.limit = requests_per_minute

    async def allow(self, principal: str) -> tuple[bool, int]:
        window = int(time.time() // 60)
        key = f"ratelimit:{principal}:{window}"
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, 90)
            count, _ = await pipe.execute()
        count = int(count)
        return count <= self.limit, max(0, self.limit - count)


class ConcurrencyLimiter(Protocol):
    async def acquire(self, backend: str, limit: int) -> bool: ...

    async def release(self, backend: str) -> None: ...


class LocalConcurrencyLimiter:
    """Single-process development limiter; production uses RedisConcurrencyLimiter."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = defaultdict(int)
        self._lock = asyncio.Lock()

    async def acquire(self, backend: str, limit: int) -> bool:
        async with self._lock:
            if self._counts[backend] >= limit:
                return False
            self._counts[backend] += 1
            return True

    async def release(self, backend: str) -> None:
        async with self._lock:
            self._counts[backend] = max(0, self._counts[backend] - 1)


class RedisConcurrencyLimiter:
    """Global in-flight admission control shared by all gateway instances.

    The key expiry is refreshed on every acquire and sits above the longest
    upstream read timeout, so counts leaked by a crashed instance self-heal
    instead of permanently shrinking capacity.
    """

    def __init__(self, redis: Redis, ttl_seconds: int = 900) -> None:
        self.redis = redis
        self.ttl_seconds = ttl_seconds

    async def acquire(self, backend: str, limit: int) -> bool:
        key = f"inflight:{backend}"
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, self.ttl_seconds)
            count, _ = await pipe.execute()
        if int(count) > limit:
            await self.redis.decr(key)
            return False
        return True

    async def release(self, backend: str) -> None:
        key = f"inflight:{backend}"
        count = await self.redis.decr(key)
        if int(count) < 0:
            await self.redis.delete(key)
