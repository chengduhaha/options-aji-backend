"""Sync options chain snapshots from Massive API into PostgreSQL + Redis."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.clients.futu_client import get_futu_client
from app.clients.massive_client import get_massive_client
from app.config import get_settings
from app.db.models import OptionsSnapshotRow
from app.utils.massive_timestamps import sip_timestamp_to_datetime
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
    last_trade = item.get("last_trade") or {}
    underlying = item.get("underlying_asset") or {}

    lt_price = last_trade.get("price")
    lt_size = last_trade.get("size")
    lt_at = sip_timestamp_to_datetime(last_trade.get("sip_timestamp"))

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
        "last_trade_price": float(lt_price) if isinstance(lt_price, (int, float)) else None,
        "last_trade_size": float(lt_size) if isinstance(lt_size, (int, float)) else None,
        "last_trade_at": lt_at,
        "snapshot_time": datetime.now(timezone.utc),
    }


def _parse_futu_contract(item: dict) -> dict:
    """Map Futu normalized option contract dict into DB-ready fields."""
    return {
        "ticker": item.get("ticker", ""),
        "underlying_ticker": item.get("underlying", ""),
        "contract_type": item.get("contract_type"),
        "expiration_date": item.get("expiration_date"),
        "strike_price": item.get("strike_price"),
        "delta": item.get("delta"),
        "gamma": item.get("gamma"),
        "theta": item.get("theta"),
        "vega": item.get("vega"),
        "implied_volatility": item.get("implied_volatility"),
        "open_interest": item.get("open_interest"),
        "bid": item.get("bid"),
        "ask": item.get("ask"),
        "bid_size": item.get("bid_size"),
        "ask_size": item.get("ask_size"),
        "midpoint": item.get("midpoint"),
        "day_open": item.get("day_open"),
        "day_high": item.get("day_high"),
        "day_low": item.get("day_low"),
        "day_close": item.get("day_close"),
        "day_volume": item.get("day_volume"),
        "day_vwap": item.get("day_vwap"),
        "day_change": item.get("day_change"),
        "day_change_pct": item.get("day_change_pct"),
        "previous_close": item.get("previous_close"),
        "break_even_price": item.get("break_even_price"),
        "underlying_price": item.get("underlying_price"),
        "last_trade_price": item.get("last_trade_price"),
        "last_trade_size": item.get("last_trade_size"),
        "last_trade_at": item.get("last_trade_at"),
        "snapshot_time": datetime.now(timezone.utc),
    }


def _sync_symbol_options(
    session,
    symbol: str,
    *,
    massive_client,
    futu_client,
    cfg,
) -> int:
    """Sync one underlying's option chain; returns number of rows upserted."""
    source = "massive"
    snapshots: list = []
    rows: list[dict] = []

    if futu_client is not None:
        futu_payload = futu_client.get_option_chain_snapshot(symbol, limit=2500)
        if not futu_payload.get("error") and futu_payload.get("contracts"):
            snapshots = list(futu_payload.get("contracts") or [])
            rows = [_parse_futu_contract(s) for s in snapshots if s.get("ticker")]
            source = "futu"
        elif massive_client is not None:
            snapshots = massive_client.get_option_chain_snapshot(
                symbol,
                max_contracts=2500,
                max_pages=80,
            )
            rows = [_parse_snapshot(s) for s in snapshots if s.get("details", {}).get("ticker")]
        else:
            logger.debug("No Futu options returned for %s and Massive is not configured", symbol)
            return 0
    elif massive_client is not None:
        snapshots = massive_client.get_option_chain_snapshot(
            symbol,
            max_contracts=2500,
            max_pages=80,
        )
        rows = [_parse_snapshot(s) for s in snapshots if s.get("details", {}).get("ticker")]
    else:
        return 0

    if not snapshots:
        logger.debug("No snapshots returned for %s", symbol)
        return 0

    for row_data in rows:
        ticker = row_data.pop("ticker")
        existing = session.get(OptionsSnapshotRow, ticker)
        if existing:
            for k, v in row_data.items():
                setattr(existing, k, v)
        else:
            session.add(OptionsSnapshotRow(ticker=ticker, **row_data))

    session.commit()
    cache_delete_pattern(f"options:chain:{symbol}*")
    chain_data = {
        "symbol": symbol,
        "source": source,
        "count": len(snapshots),
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "contracts": snapshots,
    }
    cache_set(
        key_options_chain(symbol),
        chain_data,
        ttl=cfg.futu_cache_ttl_seconds if source == "futu" else TTL_HOT,
    )
    logger.info("Options chain sync: %s via %s -> %d contracts", symbol, source, len(rows))
    return len(rows)


def sync_options_chain_pipeline() -> None:
    """Pull option chain snapshots for watchlist / S&P500 batch and upsert to DB."""
    cfg = get_settings()
    use_futu_background = bool(
        cfg.futu_enabled and getattr(cfg, "futu_background_options_sync_enabled", False)
    )
    if not use_futu_background and not cfg.massive_api_key:
        logger.debug("Neither Futu nor Massive option source is enabled, skipping options chain sync")
        return

    if cfg.sync_sp500_enabled and cfg.fmp_api_key:
        from app.sync.sp500_symbols import next_sp500_batch

        symbols = next_sp500_batch(cfg.sync_sp500_batch_size)
        scope = "sp500_batch"
    else:
        symbols = cfg.sync_watchlist_symbols
        scope = "watchlist"

    massive_client = get_massive_client() if cfg.massive_api_key else None
    futu_client = get_futu_client() if use_futu_background else None
    session = SessionLocal()

    total_upserted = 0
    try:
        for symbol in symbols:
            try:
                total_upserted += _sync_symbol_options(
                    session,
                    symbol,
                    massive_client=massive_client,
                    futu_client=futu_client,
                    cfg=cfg,
                )
            except Exception as exc:
                session.rollback()
                logger.warning("Options chain sync failed for %s: %s", symbol, exc)

    finally:
        session.close()

    logger.info(
        "Options chain sync complete (%s): %d symbols, %d total upserts",
        scope,
        len(symbols),
        total_upserted,
    )
