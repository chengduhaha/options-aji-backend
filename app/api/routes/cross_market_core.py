"""Cross-market: quote, hot events, arbitrage scanner."""
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
from app.cross_market import ibkr_connection as ibkr_conn
from app.cross_market.db_async import SessionLocal as OntologySessionLocal
from app.cross_market.domain.probability import ProbabilityInputs, fuse_probabilities
from app.cross_market.ibkr_client import get_stock_quote_ibkr
from app.cross_market.market_intelligence import enrich_probabilities_for_market
from app.cross_market.ontology_registry import ontology
from app.cross_market.persistence import replace_arbitrage_signals, save_trace_record, upsert_event_snapshots
from app.cross_market.polymarket_client import PolymarketClient
from app.cross_market.trace_store import add_trace

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

    if ibkr_conn.ibkr_is_enabled():
        await ibkr_conn.ibkr_ensure_connected()
        if ibkr_conn.ibkr_is_connected():
            q = await get_stock_quote_ibkr(upper)
            if q.get("ok"):
                last_v = q.get("last")
                bid_v = q.get("bid")
                ask_v = q.get("ask")
                close_v = q.get("close") or 0.0
                mid = None
                if bid_v is not None and ask_v is not None:
                    mid = (float(bid_v) + float(ask_v)) / 2.0
                price = float(last_v) if last_v is not None else (mid or 0.0)
                change_pct = 0.0
                if close_v and price:
                    change_pct = (price - float(close_v)) / float(close_v) * 100.0
                vol = int(q.get("volume") or 0)
                return MarketQuoteResponse(
                    symbol=str(q.get("symbol") or upper),
                    price=round(price, 4),
                    change_pct=round(change_pct, 4),
                    iv_rank=0.0,
                    volume=vol,
                    market_cap=0.0,
                    bid=round(float(bid_v), 4) if bid_v is not None else None,
                    ask=round(float(ask_v), 4) if ask_v is not None else None,
                    high=round(float(q["high"]), 4) if q.get("high") is not None else None,
                    low=round(float(q["low"]), 4) if q.get("low") is not None else None,
                    data_source="ibkr",
                )

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


class EventProbabilities(BaseModel):
    options: float = Field(ge=0, le=1)
    polymarket: float = Field(ge=0, le=1)
    social: float = Field(ge=0, le=1)
    institutional: float = Field(ge=0, le=1)


class HotEventItem(BaseModel):
    event_id: str
    title_zh: str
    event_type: str
    event_time: str
    probabilities: EventProbabilities
    consensus: float
    disagreement: float
    arbitrage_direction: str


class HotEventsResponse(BaseModel):
    events: list[HotEventItem]


@router.get("/events/hot", response_model=HotEventsResponse)
async def get_events_hot() -> HotEventsResponse:
    client = PolymarketClient()
    events: list[HotEventItem] = []
    try:
        markets = await client.search_markets(active=True, limit=12)
        # Parallel enrichment using asyncio.gather
        coros = [enrich_probabilities_for_market(market) for market in markets[:6]]
        enriched_list = await asyncio.gather(*coros)
        for market, enriched in zip(markets[:6], enriched_list):
            market_id = str(market.get("id") or "unknown")
            question = str(market.get("question") or "未知事件")
            fused = fuse_probabilities(
                ProbabilityInputs(
                    options_prob=enriched["options_probability"],
                    polymarket_prob=enriched["polymarket_probability"],
                    social_prob=enriched["social_probability"],
                    institutional_prob=enriched["institutional_probability"],
                )
            )
            events.append(
                HotEventItem(
                    event_id=f"Event:polymarket-{market_id}",
                    title_zh=question,
                    event_type="geopolitical" if "war" in question.lower() else "macro_release",
                    event_time=str(market.get("endDate") or market.get("end_date") or ""),
                    probabilities=EventProbabilities(
                        options=round(enriched["options_probability"], 3),
                        polymarket=round(enriched["polymarket_probability"], 3),
                        social=round(enriched["social_probability"], 3),
                        institutional=round(enriched["institutional_probability"], 3),
                    ),
                    consensus=fused.consensus_probability,
                    disagreement=fused.disagreement,
                    arbitrage_direction=fused.arbitrage_direction,
                )
            )
    except Exception:
        logger.exception("cross-market events hot failed")
    finally:
        await client.close()

    trace = add_trace(
        source="events.hot",
        query="scan polymarket active markets",
        matched_pattern=ontology.match_pattern_for_trigger({"content": "macro war sanction"})
        or "probability_divergence",
        used_objects=["Event", "PolymarketContract", "CrossMarketArbitrage"],
        used_relations=["event_priced_by_polymarket", "event_has_arbitrage"],
    )
    if OntologySessionLocal is not None:
        try:
            async with OntologySessionLocal() as session:
                await upsert_event_snapshots(session, [e.model_dump() for e in events])
                await save_trace_record(session, trace)
        except Exception:
            logger.debug("ontology persistence skipped", exc_info=True)
    return HotEventsResponse(events=events)


class ArbitrageOpportunity(BaseModel):
    event_id: str
    question: str
    options_probability: float = Field(ge=0, le=1)
    polymarket_probability: float = Field(ge=0, le=1)
    social_probability: float = Field(ge=0, le=1)
    institutional_probability: float = Field(ge=0, le=1)
    consensus_probability: float = Field(ge=0, le=1)
    disagreement: float
    arbitrage_direction: str
    confidence_score: float


class ArbitrageScanResponse(BaseModel):
    opportunities: list[ArbitrageOpportunity]


async def _scan_arbitrage_impl(threshold: float) -> ArbitrageScanResponse:
    client = PolymarketClient()
    opportunities: list[ArbitrageOpportunity] = []
    try:
        markets = await client.search_markets(active=True, limit=20)
        for market in markets:
            market_id = str(market.get("id") or "unknown")
            question = str(market.get("question") or "unknown event")
            liquidity = float(market.get("liquidity") or 0)
            volume = float(market.get("volume24hr") or 0)
            enriched = await enrich_probabilities_for_market(market)
            fused = fuse_probabilities(
                ProbabilityInputs(
                    options_prob=enriched["options_probability"],
                    polymarket_prob=enriched["polymarket_probability"],
                    social_prob=enriched["social_probability"],
                    institutional_prob=enriched["institutional_probability"],
                )
            )
            if fused.disagreement < threshold:
                continue
            confidence = min(
                max(min(liquidity / 250000, 1) * 0.6 + min(volume / 100000, 1) * 0.4, 0),
                1,
            )
            opportunities.append(
                ArbitrageOpportunity(
                    event_id=f"Event:poly-{market_id}",
                    question=question,
                    options_probability=round(enriched["options_probability"], 3),
                    polymarket_probability=round(enriched["polymarket_probability"], 3),
                    social_probability=round(enriched["social_probability"], 3),
                    institutional_probability=round(enriched["institutional_probability"], 3),
                    consensus_probability=fused.consensus_probability,
                    disagreement=fused.disagreement,
                    arbitrage_direction=fused.arbitrage_direction,
                    confidence_score=round(confidence, 3),
                )
            )
    except Exception:
        logger.exception("scanner arbitrage failed")
    finally:
        await client.close()

    opportunities.sort(key=lambda item: item.disagreement, reverse=True)
    top = opportunities[:10]
    trace = add_trace(
        source="scanner.arbitrage",
        query=f"periodic divergence scan threshold={threshold}",
        matched_pattern=ontology.match_pattern_for_trigger({"content": "divergence scan macro"})
        or "crossover_probability_divergence",
        used_objects=["Event", "PolymarketContract", "CrossMarketArbitrage"],
        used_relations=["options_disagrees_with_polymarket", "event_has_arbitrage"],
    )
    if OntologySessionLocal is not None:
        try:
            async with OntologySessionLocal() as session:
                await replace_arbitrage_signals(session, [item.model_dump() for item in top])
                await save_trace_record(session, trace)
        except Exception:
            pass
    return ArbitrageScanResponse(opportunities=top)


@router.get("/scanner/arbitrage", response_model=ArbitrageScanResponse)
async def scan_arbitrage_cross(
    threshold: float = Query(default=0.15, ge=0.05, le=0.5),
) -> ArbitrageScanResponse:
    return await _scan_arbitrage_impl(threshold)


# ── Feed (composed) ───────────────────────────────────────────────────────────
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
    events = await get_events_hot()
    try:
        scans = await asyncio.wait_for(_scan_arbitrage_impl(0.12), timeout=55.0)
    except asyncio.TimeoutError:
        logger.warning("cross-market feed: arbitrage scan timed out")
        scans = ArbitrageScanResponse(opportunities=[])

    items: list[FeedItem] = []
    for event in events.events:
        items.append(
            FeedItem(
                item_id=f"feed-event-{event.event_id}",
                kind="event",
                source="Polymarket+4源融合",
                timestamp=now,
                title=event.title_zh,
                sentiment="bullish" if event.probabilities.polymarket > 0.55 else "neutral",
                urgency="important" if event.disagreement > 0.15 else "normal",
                affected_tickers=[],
                ai_summary_zh=(
                    f"该事件当前共识概率 {event.consensus:.0%}，"
                    f"背离 {event.disagreement:.0%}，重点关注 {event.arbitrage_direction}。"
                ),
            )
        )
    for opp in scans.opportunities[:8]:
        items.append(
            FeedItem(
                item_id=f"feed-opp-{opp.event_id}",
                kind="signal",
                source="ArbitrageScanner",
                timestamp=now,
                title=opp.question,
                sentiment="bearish" if "underpriced" in opp.arbitrage_direction else "bullish",
                urgency="urgent" if opp.disagreement > 0.2 else "important",
                affected_tickers=[],
                ai_summary_zh=(
                    f"检测到跨市场概率背离 {opp.disagreement:.0%}，"
                    f"方向 {opp.arbitrage_direction}，置信度 {opp.confidence_score:.0%}。"
                ),
            )
        )

    items.sort(key=lambda item: (item.urgency, item.timestamp), reverse=True)
    return FeedResponse(items=items[:20])
