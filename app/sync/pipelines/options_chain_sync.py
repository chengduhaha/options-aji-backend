"""Sync options chain snapshots from Massive API into PostgreSQL + Redis."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.clients.massive_client import get_massive_client
from app.config import get_settings
from app.db.models import OptionsSnapshotRow
from app.db.session import SessionLocal
from app.services.cache_service import (
    TTL_HOT, cache_set, cache_delete_pattern,
    key_options_chain, key_gex
)

logger = logging.getLogger(__name__)


def _parse_snapshot(item: dict) -> dict:
    """Flatten Massive snapshot dict into DB-ready fields."""
    details = item.get("details", {})
    greeks = item.get("greeks") or {}
    day = item.get("day") or {}
    last_quote = item.get("last_quote") or {}
    underlying = item.get("underlying_asset") or {}

    return {
        "ticker": details.get("ticker", ""),
        "underlying_ticker": underlying.get("ticker", "") or details.get("ticker", "")[:4],
        "contract_type": details.get("contract_type"),
        "expiration_date": details.get("expiration_date"),
        "strike_price": details.get("strike_price"),
        "delta": greeks.get("delta"),
        "gamma": greeks.get("gamma"),
        "theta": greeks.get("theta"),
        "vega": greeks.get("vega"),
        "implied_volatility": item.get("implied_volatility"),
        "open_interest": item.get("open_interest"),
        "bid": last_quote.get("bid"),
        "ask": last_quote.get("ask"),
        "bid_size": last_quote.get("bid_size"),
        "ask_size": last_quote.get("ask_size"),
        "midpoint": last_quote.get("midpoint"),
        "day_open": day.get("open"),
        "day_high": day.get("high"),
        "day_low": day.get("low"),
        "day_close": day.get("close"),
        "day_volume": day.get("volume"),
        "day_vwap": day.get("vwap"),
        "day_change": day.get("change"),
        "day_change_pct": day.get("change_percent"),
        "previous_close": day.get("previous_close"),
        "break_even_price": item.get("break_even_price"),
        "underlying_price": underlying.get("price"),
        "snapshot_time": datetime.now(timezone.utc),
    }


def sync_options_chain_pipeline() -> None:
    """Pull option chain snapshots for watchlist symbols and upsert to DB."""
    cfg = get_settings()
    if not cfg.massive_api_key:
        logger.debug("MASSIVE_API_KEY not set, skipping options chain sync")
        return

    symbols = cfg.sync_watchlist_symbols
    client = get_massive_client()
    session = SessionLocal()

    total_upserted = 0
    try:
        for symbol in symbols:
            try:
                snapshots = client.get_option_chain_snapshot(
                    symbol,
                    max_contracts=2500,
                    max_pages=80,
                )
                if not snapshots:
                    logger.debug("No snapshots returned for %s", symbol)
                    continue

                rows = [_parse_snapshot(s) for s in snapshots if s.get("details", {}).get("ticker")]

                # Bulk upsert
                for row_data in rows:
                    ticker = row_data.pop("ticker")
                    existing = session.get(OptionsSnapshotRow, ticker)
                    if existing:
                        for k, v in row_data.items():
                            setattr(existing, k, v)
                    else:
                        session.add(OptionsSnapshotRow(ticker=ticker, **row_data))

                session.commit()
                total_upserted += len(rows)

                # Invalidate Redis cache for this symbol's chain
                cache_delete_pattern(f"options:chain:{symbol}*")

                # Build and cache the chain JSON for frontend
                chain_data = {
                    "symbol": symbol,
                    "count": len(snapshots),
                    "synced_at": datetime.now(timezone.utc).isoformat(),
                    "contracts": snapshots,
                }
                cache_set(key_options_chain(symbol), chain_data, ttl=TTL_HOT)

                logger.info("Options chain sync: %s -> %d contracts", symbol, len(rows))

            except Exception as exc:
                session.rollback()
                logger.warning("Options chain sync failed for %s: %s", symbol, exc)

    finally:
        session.close()

    logger.info("Options chain sync complete: %d total upserts", total_upserted)
