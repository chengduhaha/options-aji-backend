"""Massive snapshot helpers matching Polygon-style `results` list."""
from __future__ import annotations

import asyncio
import logging

from app.clients.massive_client import get_massive_client
from app.config import get_settings

logger = logging.getLogger(__name__)


async def get_options_chain_results(underlying: str, *, max_contracts: int = 250, max_pages: int = 5) -> dict:
    """Return `{"results": [...]}` compatible with cross-market intelligence."""

    def _run() -> dict:
        cfg = get_settings()
        if not cfg.massive_api_key:
            return {"results": []}
        try:
            rows = get_massive_client().get_option_chain_snapshot(
                underlying.upper(),
                max_contracts=max_contracts,
                max_pages=max_pages,
            )
            return {"results": rows}
        except Exception as exc:
            logger.warning("massive chain %s: %s", underlying, exc)
            return {"results": []}

    return await asyncio.to_thread(_run)
