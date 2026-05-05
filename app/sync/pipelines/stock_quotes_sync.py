"""Sync stock quotes from FMP batch endpoint into PostgreSQL + Redis."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.clients.fmp_client import get_fmp_client
from app.config import get_settings
from app.db.models import StockQuoteRow
from app.db.session import SessionLocal
from app.services.cache_service import TTL_HOT, cache_set, key_stock_quote

logger = logging.getLogger(__name__)


def sync_stock_quotes_pipeline() -> None:
    """Fetch batch quotes for watchlist symbols, upsert DB, refresh Redis."""
    cfg = get_settings()
    if not cfg.fmp_api_key:
        logger.debug("FMP_API_KEY not set, skipping stock quotes sync")
        return

    symbols = cfg.sync_watchlist_symbols
    client = get_fmp_client()

    # FMP batch-quote-short supports up to 100 symbols
    batch_size = 50
    session = SessionLocal()
    try:
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i : i + batch_size]
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

                existing = session.get(StockQuoteRow, symbol)
                if existing:
                    for k, v in row_data.items():
                        setattr(existing, k, v)
                else:
                    session.add(StockQuoteRow(symbol=symbol, **row_data))

                # Update Redis
                cache_data = {"symbol": symbol, **row_data, "synced_at": datetime.now(timezone.utc).isoformat()}
                cache_set(key_stock_quote(symbol), cache_data, ttl=TTL_HOT)

            session.commit()
            logger.info("Stock quotes sync: batch %d-%d done", i, i + len(batch))

    except Exception as exc:
        session.rollback()
        logger.error("Stock quotes sync failed: %s", exc)
    finally:
        session.close()
