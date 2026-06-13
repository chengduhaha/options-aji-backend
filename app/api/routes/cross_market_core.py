"""Cross-market: Polymarket + Xpoz US-equity hotspots (independent feeds)."""
from __future__ import annotations

import asyncio
import logging
import re

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.clients.fmp_client import get_fmp_client
from app.clients.futu_client import get_futu_client
from app.config import get_settings
from app.cross_market.db_async import SessionLocal as CrossMarketSessionLocal
from app.cross_market.persistence import upsert_event_snapshots
from app.cross_market.polymarket_client import PolymarketClient
from app.cross_market.us_equity_markets import fetch_us_equity_markets, market_to_hot_fields
from app.cross_market.xpoz_ticker_detail import XpozTickerDetailResponse, fetch_xpoz_ticker_detail
from app.cross_market.xpoz_us_hot import XpozHotResponse, fetch_xpoz_us_hot

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cross-market", tags=["cross-market"])

_RESERVED_SYMBOLS = frozenset(
    {"overview", "chain", "health", "quote", "summary", "index", "v3", "api", "feed", "events", "scanner"},
)
_TICKER_RE = re.compile(r"^[A-Za-z]{1,5}([.\-][A-Za-z])?$")


class MarketQuoteResponse(BaseModel):
    symbol: str
    price: float
    change_pct: float
    iv_rank: float
    volume: int
    market_cap: float
    bid: float | None = None
    ask: float | None = None
    high: float | None = None
    low: float | None = None
    data_source: str = "fmp"


def _empty_quote(symbol: str) -> MarketQuoteResponse:
    return MarketQuoteResponse(
        symbol=symbol,
        price=0,
        change_pct=0,
        iv_rank=0,
        volume=0,
        market_cap=0,
        data_source="none",
    )


@router.get("/quote/{symbol}", response_model=MarketQuoteResponse)
async def cross_market_quote(symbol: str) -> MarketQuoteResponse:
    raw = symbol.strip()
    if not raw:
        return _empty_quote("N/A")
    upper = raw.upper()
    if raw.lower() in _RESERVED_SYMBOLS or not _TICKER_RE.match(raw):
        raise HTTPException(status_code=422, detail="Invalid equity symbol for quote endpoint.")

    settings = get_settings()
    if getattr(settings, "futu_enabled", False):
        def _futu() -> MarketQuoteResponse:
            row = get_futu_client().get_stock_quote(upper)
            if row.get("error"):
                return _empty_quote(upper).model_copy(update={"data_source": "futu"})
            return MarketQuoteResponse(
                symbol=str(row.get("symbol") or upper),
                price=float(row.get("last_price") or 0),
                change_pct=float(row.get("change_pct") or 0),
                iv_rank=0.0,
                volume=int(row.get("volume") or 0),
                market_cap=float(row.get("market_cap") or 0),
                high=float(row["day_high"]) if row.get("day_high") is not None else None,
                low=float(row["day_low"]) if row.get("day_low") is not None else None,
                data_source="futu",
            )

        futu_quote = await asyncio.to_thread(_futu)
        if futu_quote.price > 0:
            return futu_quote

    def _fmp() -> MarketQuoteResponse:
        row = get_fmp_client().get_quote(upper)
        if not row:
            return _empty_quote(upper).model_copy(update={"data_source": "fmp"})
        return MarketQuoteResponse(
            symbol=str(row.get("symbol") or upper),
            price=float(row.get("price") or 0),
            change_pct=float(row.get("changesPercentage") or row.get("changePercentage") or 0),
            iv_rank=50.0,
            volume=int(row.get("volume") or 0),
            market_cap=float(row.get("marketCap") or 0),
            data_source="fmp",
        )

    return await asyncio.to_thread(_fmp)


class HotEventItem(BaseModel):
    event_id: str
    title_zh: str
    event_type: str
    event_time: str
    polymarket_probability: float = Field(ge=0, le=1)
    related_ticker: str | None = None
    related_tickers: list[str] = Field(default_factory=list)
    volume_24h: float | None = None
    liquidity: float | None = None
    slug: str | None = None


class HotEventsResponse(BaseModel):
    events: list[HotEventItem]


async def _load_us_equity_hot_events(limit: int) -> list[HotEventItem]:
    client = PolymarketClient()
    events: list[HotEventItem] = []
    try:
        markets = await fetch_us_equity_markets(client, limit=limit)
        for market in markets:
            market_id = str(market.get("id") or "unknown")
            fields = market_to_hot_fields(market)
            events.append(
                HotEventItem(
                    event_id=f"Event:polymarket-{market_id}",
                    **fields,
                )
            )
    except Exception:
        logger.exception("cross-market US equity hot events failed")
    finally:
        await client.close()
    return events


@router.get("/events/hot", response_model=HotEventsResponse)
@router.get("/polymarket/hot", response_model=HotEventsResponse)
async def get_events_hot(
    limit: int = Query(default=20, ge=1, le=30),
) -> HotEventsResponse:
    events = await _load_us_equity_hot_events(limit)

    if CrossMarketSessionLocal is not None:
        try:
            async with CrossMarketSessionLocal() as session:
                await upsert_event_snapshots(session, [e.model_dump() for e in events])
        except Exception:
            logger.debug("cross-market persistence skipped", exc_info=True)
    return HotEventsResponse(events=events)


@router.get("/xpoz/hot", response_model=XpozHotResponse)
async def get_xpoz_hot(
    limit: int = Query(default=15, ge=1, le=30),
) -> XpozHotResponse:
    return await fetch_xpoz_us_hot(limit=limit)


@router.get("/xpoz/ticker/{symbol}", response_model=XpozTickerDetailResponse)
async def get_xpoz_ticker_detail(symbol: str) -> XpozTickerDetailResponse:
    raw = symbol.strip().upper()
    if not raw or not _TICKER_RE.match(raw):
        raise HTTPException(status_code=422, detail="Invalid equity symbol.")
    return await fetch_xpoz_ticker_detail(raw)
