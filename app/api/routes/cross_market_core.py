"""Cross-market: quote, US-equity Polymarket hotspots, feed."""
from __future__ import annotations

import asyncio
import datetime as dt
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
async def get_events_hot(
    limit: int = Query(default=12, ge=1, le=30),
) -> HotEventsResponse:
    events = await _load_us_equity_hot_events(limit)

    if CrossMarketSessionLocal is not None:
        try:
            async with CrossMarketSessionLocal() as session:
                await upsert_event_snapshots(session, [e.model_dump() for e in events])
        except Exception:
            logger.debug("cross-market persistence skipped", exc_info=True)
    return HotEventsResponse(events=events)


class ArbitrageOpportunity(BaseModel):
    """Legacy scanner row — now PM-only US equity hotspot (no cross-source divergence)."""

    event_id: str
    question: str
    polymarket_probability: float = Field(ge=0, le=1)
    related_ticker: str | None = None
    volume_24h: float | None = None
    liquidity: float | None = None
    slug: str | None = None
    event_type: str = "equity"


class ArbitrageScanResponse(BaseModel):
    opportunities: list[ArbitrageOpportunity]


@router.get("/scanner/arbitrage", response_model=ArbitrageScanResponse)
async def scan_arbitrage_cross(
    limit: int = Query(default=20, ge=1, le=30),
) -> ArbitrageScanResponse:
    """US-equity Polymarket markets sorted by volume (no divergence scan)."""
    hot = await _load_us_equity_hot_events(limit)
    opportunities = [
        ArbitrageOpportunity(
            event_id=item.event_id.replace("Event:polymarket-", "Event:poly-"),
            question=item.title_zh,
            polymarket_probability=item.polymarket_probability,
            related_ticker=item.related_ticker,
            volume_24h=item.volume_24h,
            liquidity=item.liquidity,
            slug=item.slug,
            event_type=item.event_type,
        )
        for item in hot
    ]
    return ArbitrageScanResponse(opportunities=opportunities)


class FeedItem(BaseModel):
    item_id: str
    kind: str
    source: str
    timestamp: str
    title: str
    sentiment: str
    urgency: str
    affected_tickers: list[str]
    ai_summary_zh: str


class FeedResponse(BaseModel):
    items: list[FeedItem]


@router.get("/feed", response_model=FeedResponse)
async def cross_market_feed() -> FeedResponse:
    now = dt.datetime.now(tz=dt.timezone.utc).isoformat()
    events = await _load_us_equity_hot_events(15)

    items: list[FeedItem] = []
    for event in events:
        tickers = [event.related_ticker] if event.related_ticker else []
        pm_pct = event.polymarket_probability * 100
        vol_hint = f"，24h 成交 {event.volume_24h:,.0f}" if event.volume_24h else ""
        ticker_hint = f"关联标的 {event.related_ticker}" if event.related_ticker else "宏观/主题市场"
        items.append(
            FeedItem(
                item_id=f"feed-event-{event.event_id}",
                kind="event",
                source="Polymarket",
                timestamp=now,
                title=event.title_zh,
                sentiment="bullish" if event.polymarket_probability > 0.55 else "neutral",
                urgency="important" if (event.volume_24h or 0) > 50_000 else "normal",
                affected_tickers=tickers,
                ai_summary_zh=(
                    f"预测市场 Yes 概率 {pm_pct:.1f}%{vol_hint}。{ticker_hint}。"
                ),
            )
        )

    return FeedResponse(items=items[:20])
