"""Redis client: rate-limit buckets, idempotency keys, short-lived query cache."""

from __future__ import annotations

import logging

from redis.asyncio import Redis

from app.config import Settings

log = logging.getLogger("lens.request")

_redis: Redis | None = None


def init_cache(settings: Settings) -> Redis:
    global _redis
    if _redis is None:
        _redis = Redis.from_url(
            settings.redis_url, decode_responses=True, socket_timeout=1.0, socket_connect_timeout=1.0
        )
    return _redis


def get_redis() -> Redis | None:
    return _redis


async def close_cache() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
    _redis = None


async def cache_ping() -> bool:
    if _redis is None:
        return False
    try:
        return bool(await _redis.ping())
    except Exception:
        return False
