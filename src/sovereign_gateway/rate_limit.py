from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import Protocol

from redis.asyncio import Redis


class RateLimiter(Protocol):
    # The configured per-minute request budget. Callers read it (e.g. to size
    # the degraded-mode fallback window), so it is part of the contract, not an
    # incidental attribute of the concrete limiters.
    limit: int

    async def allow(self, principal: str) -> tuple[bool, int]: ...


class LocalFixedWindowLimiter:
    """Single-process development / degraded-mode limiter; production uses
    RedisFixedWindowLimiter.

    Stale windows are swept once the map grows past a threshold. The
    failed-auth throttle routes unauthenticated, attacker-controlled bucket
    names (one per unique invalid key) here when Redis is down, so without the
    sweep the map would retain every bucket ever seen. Production's Redis path
    expires keys after 90s instead; within a single 60s window growth is
    bounded by the edge request rate, and each window roll lets the sweep
    reclaim the previous window's buckets.
    """

    _SWEEP_THRESHOLD = 100_000

    def __init__(self, requests_per_minute: int) -> None:
        self.limit = requests_per_minute
        self._windows: dict[str, tuple[int, int]] = {}
        self._lock = asyncio.Lock()

    async def allow(self, principal: str) -> tuple[bool, int]:
        window = int(time.time() // 60)
        async with self._lock:
            if len(self._windows) >= self._SWEEP_THRESHOLD:
                self._windows = {
                    key: value for key, value in self._windows.items() if value[0] == window
                }
            stored_window, count = self._windows.get(principal, (window, 0))
            count = count + 1 if stored_window == window else 1
            self._windows[principal] = (window, count)
        return count <= self.limit, max(0, self.limit - count)


class RedisFixedWindowLimiter:
    def __init__(
        self, redis: Redis, requests_per_minute: int, *, namespace: str = "ratelimit"
    ) -> None:
        self.redis = redis
        self.limit = requests_per_minute
        self.namespace = namespace

    async def allow(self, principal: str) -> tuple[bool, int]:
        window = int(time.time() // 60)
        key = f"{self.namespace}:{principal}:{window}"
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
