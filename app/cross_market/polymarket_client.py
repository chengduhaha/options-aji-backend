"""Polymarket Gamma API client."""
from __future__ import annotations

from typing import Optional

import httpx

from app.cross_market.redis_cache import cache_get_json, cache_set_json

GAMMA_BASE = "https://gamma-api.polymarket.com"


class PolymarketClient:
    def __init__(self) -> None:
        self.client = httpx.AsyncClient(base_url=GAMMA_BASE, timeout=15.0)

    async def search_markets(
        self,
        query: Optional[str] = None,
        category: Optional[str] = None,
        active: bool = True,
        limit: int = 20,
    ) -> list[dict]:
        cache_key = f"cm:poly:markets:{query or 'all'}:{category or 'all'}:{active}:{limit}"
        cached = await cache_get_json(cache_key)
        if cached is not None:
            return cached
        params: dict[str, str | int] = {
            "active": str(active).lower(),
            "limit": limit,
            "closed": "false",
        }
        if query:
            params["q"] = query
        if category:
            params["category"] = category
        response = await self.client.get("/markets", params=params)
        response.raise_for_status()
        payload = response.json()
        await cache_set_json(cache_key, payload, ttl_seconds=60)
        return payload

    async def get_market(self, market_id: str) -> dict:
        cache_key = f"cm:poly:market:{market_id}"
        cached = await cache_get_json(cache_key)
        if cached is not None:
            return cached
        response = await self.client.get(f"/markets/{market_id}")
        response.raise_for_status()
        payload = response.json()
        await cache_set_json(cache_key, payload, ttl_seconds=60)
        return payload

    async def close(self) -> None:
        await self.client.aclose()
