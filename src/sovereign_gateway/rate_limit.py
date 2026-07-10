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
