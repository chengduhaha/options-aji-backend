"""Async Redis helpers for cross-market API caching."""
from __future__ import annotations

import json
import logging
from typing import Any

from redis.asyncio import Redis, from_url

from app.config import get_settings

logger = logging.getLogger(__name__)
_redis: Redis | None = None


def _client() -> Redis:
    global _redis
    if _redis is None:
        settings = get_settings()
        _redis = from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=3,
            retry_on_timeout=True,
            health_check_interval=30,
        )
    return _redis


async def cache_get_json(key: str) -> Any | None:
    try:
        payload = await _client().get(key)
    except Exception:
        logger.debug("redis get failed key=%s", key, exc_info=True)
        return None
    if not payload:
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None


async def cache_set_json(key: str, value: Any, ttl_seconds: int) -> None:
    try:
        await _client().set(
            key,
            json.dumps(value, ensure_ascii=False),
            ex=ttl_seconds,
        )
    except Exception:
        logger.debug("redis set failed key=%s", key, exc_info=True)


async def ping_redis() -> bool:
    try:
        pong = await _client().ping()
        return bool(pong)
    except Exception:
        return False
