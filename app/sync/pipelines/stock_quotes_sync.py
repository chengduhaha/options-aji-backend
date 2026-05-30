"""Sync stock quotes from FMP batch endpoint into PostgreSQL + Redis."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.clients.fmp_client import get_fmp_client
from app.clients.futu_client import get_futu_client
from app.config import get_settings
from app.db.models import StockQuoteRow
from app.db.session import SessionLocal
from app.services.cache_service import TTL_HOT, cache_set, key_stock_quote

logger = logging.getLogger(__name__)


def _upsert_stock_quote(session, symbol: str, row_data: dict) -> None:
    existing = session.get(StockQuoteRow, symbol)
    if existing:
        for key, value in row_data.items():
            setattr(existing, key, value)
    else:
        session.add(StockQuoteRow(symbol=symbol, **row_data))


def _cache_stock_quote(symbol: str, row_data: dict) -> None:
    cache_data = {"symbol": symbol, **row_data, "synced_at": datetime.now(timezone.utc).isoformat()}
    cache_set(key_stock_quote(symbol), cache_data, ttl=TTL_HOT)


def _row_data_from_futu_quote(quote: dict) -> dict:
    return {
        "price": quote.get("last_price"),
        "change": quote.get("change"),
        "change_pct": quote.get("change_pct"),
        "day_high": quote.get("day_high"),
        "day_low": quote.get("day_low"),
        "year_high": None,
        "year_low": None,
        "volume": quote.get("volume"),
        "avg_volume": None,
        "market_cap": quote.get("market_cap"),
        "pe": quote.get("pe"),
        "eps": quote.get("eps"),
        "open_price": quote.get("regular_market_open"),
        "previous_close": quote.get("previous_close"),
        "snapshot_time": datetime.now(timezone.utc),
    }


def sync_stock_quotes_pipeline() -> None:
    """Fetch batch quotes for watchlist symbols, upsert DB, refresh Redis."""
    cfg = get_settings()
    if not cfg.futu_enabled and not cfg.fmp_api_key:
        logger.debug("Neither Futu nor FMP quote source is enabled, skipping stock quotes sync")
        return

    symbols = cfg.sync_watchlist_symbols
    session = SessionLocal()
    try:
        synced_symbols: set[str] = set()

        if cfg.futu_enabled:
            try:
                for quote in get_futu_client().get_stock_quotes(symbols):
                    if not isinstance(quote, dict) or quote.get("error"):
                        continue
                    symbol = str(quote.get("symbol") or "").upper()
                    if not symbol:
                        continue
                    row_data = _row_data_from_futu_quote(quote)
                    _upsert_stock_quote(session, symbol, row_data)
                    _cache_stock_quote(symbol, row_data)
                    synced_symbols.add(symbol)
                session.commit()
                if synced_symbols:
                    logger.info("Stock quotes sync: Futu -> %d symbols", len(synced_symbols))
            except Exception as exc:
                session.rollback()
                logger.warning("Futu stock quotes sync failed, falling back to FMP: %s", exc)

        remaining_symbols = [symbol for symbol in symbols if symbol not in synced_symbols]
        if not remaining_symbols:
            return
        if not cfg.fmp_api_key:
            logger.debug("FMP_API_KEY not set, skipping fallback stock quotes sync")
            return

        client = get_fmp_client()
        batch_size = 50
        for i in range(0, len(remaining_symbols), batch_size):
            batch = remaining_symbols[i : i + batch_size]
            quotes = client.get_batch_quote_short(batch)
            if not quotes:
                continue

            for q in quotes:
                symbol = q.get("symbol", "").upper()
                if not symbol:
                    continue

                # Get full quote details
                full = client.get_quote(symbol) or q

                row_data = {
                    "price": full.get("price") or q.get("price"),
                    "change": full.get("change"),
                    "change_pct": full.get("changesPercentage") or q.get("changesPercentage"),
                    "day_high": full.get("dayHigh"),
                    "day_low": full.get("dayLow"),
                    "year_high": full.get("yearHigh"),
                    "year_low": full.get("yearLow"),
                    "volume": full.get("volume") or q.get("volume"),
                    "avg_volume": full.get("avgVolume"),
                    "market_cap": full.get("marketCap"),
                    "pe": full.get("pe"),
                    "eps": full.get("eps"),
                    "open_price": full.get("open"),
                    "previous_close": full.get("previousClose"),
                    "snapshot_time": datetime.now(timezone.utc),
                }

                _upsert_stock_quote(session, symbol, row_data)
                _cache_stock_quote(symbol, row_data)

            session.commit()
            logger.info("Stock quotes sync: batch %d-%d done", i, i + len(batch))

    except Exception as exc:
        session.rollback()
        logger.error("Stock quotes sync failed: %s", exc)
    finally:
        session.close()
