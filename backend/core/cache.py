"""In-memory TTL cache with an optional Redis backend (REDIS_URL)."""
from __future__ import annotations

import json
import logging
import time
from typing import Any

log = logging.getLogger("cs2.cache")


class TTLCache:
    def __init__(self, max_items: int = 5000, redis_url: str | None = None, prefix: str = "cs2:") -> None:
        self._d: dict[str, tuple[float, Any]] = {}
        self.max_items = max_items
        self.prefix = prefix
        self._redis = None
        if redis_url:
            try:
                import redis.asyncio as aioredis  # type: ignore
                self._redis = aioredis.from_url(redis_url, decode_responses=True)
                log.info("Redis cache enabled")
            except Exception as e:  # redis not installed or invalid URL
                log.warning("Redis not available (%s); using the in-memory cache", e)

    async def get(self, key: str) -> Any:
        item = self._d.get(key)
        if item:
            if item[0] > time.time():
                return item[1]
            self._d.pop(key, None)
        if self._redis is not None:
            try:
                raw = await self._redis.get(self.prefix + key)
                if raw is not None:
                    return json.loads(raw)
            except Exception as e:
                log.debug("Redis get failed: %s", e)
        return None

    async def set(self, key: str, value: Any, ttl: float) -> None:
        if ttl <= 0:
            return
        if len(self._d) >= self.max_items:
            now = time.time()
            for k in [k for k, (exp, _) in self._d.items() if exp < now] or list(self._d)[: self.max_items // 10]:
                self._d.pop(k, None)
        self._d[key] = (time.time() + ttl, value)
        if self._redis is not None:
            try:
                await self._redis.set(self.prefix + key, json.dumps(value, default=str), ex=int(max(1, ttl)))
            except Exception as e:
                log.debug("Redis set failed: %s", e)

    def clear(self) -> None:
        self._d.clear()
