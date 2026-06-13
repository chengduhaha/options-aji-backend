"""Sync daily OHLCV bars for congress backtest pricing."""
from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy import select

from app.clients.fmp_client import get_fmp_client
from app.config import get_settings
from app.db.models import CongressTradeRow, StockDailyBarRow
from app.db.session import SessionLocal
from app.tools.stock_history import fetch_yfinance_daily_history

logger = logging.getLogger(__name__)


def _symbols_to_sync(session) -> list[str]:
    cfg = get_settings()
    symbols = set(cfg.sync_watchlist_symbols)
    rows = session.execute(
        select(CongressTradeRow.symbol).where(CongressTradeRow.symbol != "").distinct()
    ).scalars().all()
    for sym in rows:
        if sym:
            symbols.add(str(sym).upper())
    return sorted(symbols)


def _upsert_yfinance_bars(session, symbol: str) -> int:
    hist = fetch_yfinance_daily_history(symbol, period="2y")
    if hist is None or getattr(hist, "empty", True):
        return 0
    upserted = 0
    for idx, row in hist.iterrows():
        try:
            bar_date = idx.date() if hasattr(idx, "date") else date.fromisoformat(str(idx)[:10])
        except (ValueError, TypeError):
            continue
        close = row.get("Close")
        if close is None:
            continue
        existing = session.get(StockDailyBarRow, (symbol, bar_date))
        payload = {
            "open_price": float(row["Open"]) if row.get("Open") is not None else None,
            "high": float(row["High"]) if row.get("High") is not None else None,
            "low": float(row["Low"]) if row.get("Low") is not None else None,
            "close": float(close),
            "adj_close": float(close),
            "volume": int(row["Volume"]) if row.get("Volume") is not None else None,
        }
        if existing:
            for k, v in payload.items():
                setattr(existing, k, v)
        else:
            session.add(StockDailyBarRow(symbol=symbol, bar_date=bar_date, **payload))
        upserted += 1
    session.commit()
    return upserted


def sync_stock_daily_bars_pipeline() -> None:
    cfg = get_settings()
    session = SessionLocal()
    client = get_fmp_client() if cfg.fmp_api_key else None
    to_date = date.today().isoformat()
    from_date = (date.today() - timedelta(days=400)).isoformat()
    upserted = 0

    try:
        for symbol in _symbols_to_sync(session):
            symbol_upserted = 0
            if client is not None:
                try:
                    bars = client.get_historical_price_eod(symbol, from_date=from_date, to_date=to_date)
                    for bar in bars:
                        raw_date = str(bar.get("date") or "")[:10]
                        if not raw_date:
                            continue
                        bar_date = date.fromisoformat(raw_date)
                        existing = session.get(StockDailyBarRow, (symbol, bar_date))
                        payload = {
                            "open_price": bar.get("open"),
                            "high": bar.get("high"),
                            "low": bar.get("low"),
                            "close": bar.get("close"),
                            "adj_close": bar.get("adjClose") or bar.get("adj_close"),
                            "volume": bar.get("volume"),
                        }
                        if existing:
                            for k, v in payload.items():
                                setattr(existing, k, v)
                        else:
                            session.add(StockDailyBarRow(symbol=symbol, bar_date=bar_date, **payload))
                        symbol_upserted += 1
                    if symbol_upserted > 0:
                        session.commit()
                except Exception as exc:
                    session.rollback()
                    logger.warning("FMP stock_daily_bars sync failed for %s: %s", symbol, exc)
            if symbol_upserted == 0:
                try:
                    symbol_upserted = _upsert_yfinance_bars(session, symbol)
                except Exception as exc:
                    session.rollback()
                    logger.warning("yfinance stock_daily_bars sync failed for %s: %s", symbol, exc)
            upserted += symbol_upserted
    finally:
        session.close()

    logger.info("stock_daily_bars sync complete: %d bar rows touched", upserted)
