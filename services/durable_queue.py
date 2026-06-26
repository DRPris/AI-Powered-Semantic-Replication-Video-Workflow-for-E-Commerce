"""Redis notification queue backed by PostgreSQL job truth."""

from __future__ import annotations

import uuid

from redis.asyncio import Redis


class DurableJobQueue:
    def __init__(self, redis_url: str, queue_name: str) -> None:
        self.queue_name = queue_name
        self._redis = Redis.from_url(redis_url, decode_responses=True)

    async def enqueue(self, job_id: uuid.UUID | str) -> None:
        await self._redis.lpush(self.queue_name, str(job_id))

    async def dequeue(self, timeout_seconds: int = 5) -> uuid.UUID | None:
        item = await self._redis.brpop(self.queue_name, timeout=timeout_seconds)
        if item is None:
            return None
        _, raw_job_id = item
        return uuid.UUID(raw_job_id)

    async def ping(self) -> bool:
        return bool(await self._redis.ping())

    async def close(self) -> None:
        await self._redis.aclose()
