"""Event bus: the ball feed (producer) is decoupled from the processing
worker (consumer), exactly as a real live-scores feed would be.

- REDIS_URL set   -> Redis Streams (XADD / XREAD), e.g. Upstash free tier
- otherwise       -> in-process asyncio queues (zero-config local dev)

Swapping in Kafka means implementing the same two methods.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import AsyncIterator

from .metrics import metrics


class MemoryBus:
    name = "memory"

    def __init__(self):
        self._queues: dict[str, asyncio.Queue] = {}

    def _q(self, stream: str) -> asyncio.Queue:
        return self._queues.setdefault(stream, asyncio.Queue())

    async def publish(self, stream: str, event: dict) -> None:
        await self._q(stream).put(event)

    async def consume(self, stream: str) -> AsyncIterator[dict]:
        q = self._q(stream)
        while True:
            yield await q.get()

    async def drop(self, stream: str) -> None:
        self._queues.pop(stream, None)


class RedisStreamBus:
    name = "redis-streams"

    def __init__(self, url: str):
        import redis.asyncio as redis  # imported lazily so local dev needs no Redis
        self.r = redis.from_url(url, decode_responses=True)

    async def publish(self, stream: str, event: dict) -> None:
        await self.r.xadd(stream, {"e": json.dumps(event)}, maxlen=600, approximate=True)

    async def consume(self, stream: str) -> AsyncIterator[dict]:
        last = "0-0"
        while True:
            resp = await self.r.xread({stream: last}, block=5000, count=50)
            for _stream, entries in resp or []:
                for entry_id, fields in entries:
                    last = entry_id
                    yield json.loads(fields["e"])

    async def drop(self, stream: str) -> None:
        try:
            await self.r.delete(stream)
        except Exception:
            pass


async def make_bus():
    url = os.getenv("REDIS_URL")
    if url:
        try:
            bus = RedisStreamBus(url)
            await asyncio.wait_for(bus.r.ping(), timeout=5)
            metrics.bus_backend = bus.name
            return bus
        except Exception as e:  # fall back rather than take the demo down
            print(f"[bus] Redis unavailable ({e!r}); using in-memory bus")
    metrics.bus_backend = "memory"
    return MemoryBus()
