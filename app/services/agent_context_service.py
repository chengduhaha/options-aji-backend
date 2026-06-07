"""Agent context assembly — Redis / DB first, no live external APIs on request path."""

from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import select

from app.db.models import OptionsSnapshotRow
from app.db.session import SessionLocal
from app.services.cache_service import (
    cache_get,
    key_analyst_ratings,
    key_gex,
    key_options_chain,
    key_stock_news,
    key_stock_overview,
)

logger = logging.getLogger(__name__)


def _as_dict(value: object) -> Optional[dict[str, Any]]:
    return value if isinstance(value, dict) else None


def _spot_from_overview(overview: dict[str, Any]) -> float:
    bar = overview.get("market_bar")
    if isinstance(bar, dict):
        price = bar.get("price") or bar.get("spot")
        if isinstance(price, (int, float)) and price > 0:
            return float(price)
    quote = overview.get("quote")
    if isinstance(quote, dict):
        price = quote.get("last_price") or quote.get("price")
        if isinstance(price, (int, float)) and price > 0:
            return float(price)
    return 0.0


def _gex_from_cache(sym: str) -> tuple[Optional[dict[str, Any]], str]:
    cached = _as_dict(cache_get(key_gex(sym)))
    if not cached or cached.get("error"):
        return None, "miss"
    strikes = cached.get("strikes")
    strikes_count = len(strikes) if isinstance(strikes, list) else 0
    return {
        "netGex_bn": cached.get("netGex"),
        "regime": cached.get("regime"),
        "gammaFlip": cached.get("gammaFlip"),
        "maxPain": cached.get("maxPain"),
        "callWall": cached.get("callWall"),
        "putWall": cached.get("putWall"),
        "strikes_count": strikes_count,
        "underlyingPrice": cached.get("underlyingPrice"),
    }, "redis"


def _overview_from_cache(sym: str) -> tuple[Optional[dict[str, Any]], str]:
    cached = _as_dict(cache_get(key_stock_overview(sym)))
    if not cached:
        return None, "miss"
    bar = cached.get("bar") if isinstance(cached.get("bar"), dict) else {}
    key_stats = cached.get("keyStats") if isinstance(cached.get("keyStats"), dict) else {}
    option_liq = (
        cached.get("optionLiquidity") if isinstance(cached.get("optionLiquidity"), dict) else {}
    )
    return {
        "market_bar": bar,
        "key_stats": key_stats,
        "option_liquidity": option_liq,
        "expected_moves": cached.get("expectedMoves"),
        "quote": cached.get("quote"),
    }, "redis"


def _news_from_cache(sym: str, *, limit: int = 5) -> tuple[list[dict[str, Any]], str]:
    cached = cache_get(key_stock_news(sym))
    articles: list[dict[str, Any]] = []
    if isinstance(cached, list):
        articles = [a for a in cached if isinstance(a, dict)]
    elif isinstance(cached, dict) and isinstance(cached.get("articles"), list):
        articles = [a for a in cached["articles"] if isinstance(a, dict)]
    if not articles:
        return [], "miss"
    trimmed: list[dict[str, Any]] = []
    for article in articles[:limit]:
        content = article.get("content")
        summary = article.get("summary_zh")
        if not summary and isinstance(content, str):
            summary = content[:200]
        trimmed.append(
            {
                "title": article.get("title") or article.get("title_zh"),
                "source": article.get("source"),
                "published_at": article.get("published_at") or article.get("publishedDate"),
                "summary_zh": summary,
                "url": article.get("url"),
            }
        )
    return trimmed, "redis"


def _analyst_from_cache(sym: str) -> tuple[Optional[dict[str, Any]], str]:
    cached = _as_dict(cache_get(key_analyst_ratings(sym)))
    if not cached:
        return None, "miss"
    ratings = cached.get("ratings")
    if not isinstance(ratings, list) or not ratings:
        return None, "miss"
    return {
        "recent_ratings": ratings[:5],
        "price_target_summary": cached.get("price_target_summary"),
        "price_target_consensus": cached.get("price_target_consensus"),
    }, "redis"


def _contract_dict_from_row(row: OptionsSnapshotRow) -> dict[str, Any]:
    return {
        "type": row.contract_type,
        "strike": row.strike_price,
        "bid": row.bid,
        "ask": row.ask,
        "iv": row.implied_volatility,
        "delta": row.delta,
        "gamma": row.gamma,
        "theta": row.theta,
        "vega": row.vega,
        "oi": row.open_interest,
        "volume": row.day_volume,
    }


def _atm_options_from_db(sym: str, spot: float, *, limit: int = 6) -> tuple[list[dict[str, Any]], str]:
    if spot <= 0:
        return [], "miss"
    try:
        with SessionLocal() as session:
            rows = session.execute(
                select(OptionsSnapshotRow)
                .where(
                    OptionsSnapshotRow.underlying_ticker == sym,
                    OptionsSnapshotRow.day_volume >= 1,
                )
                .order_by(OptionsSnapshotRow.day_volume.desc())
                .limit(500)
            ).scalars().all()
    except Exception as exc:
        logger.debug("atm_options_db %s: %s", sym, exc)
        return [], "miss"

    if not rows:
        return [], "miss"

    ranked = sorted(
        rows,
        key=lambda r: abs(float(r.strike_price or 0) - spot) if r.strike_price else float("inf"),
    )[:limit]
    return [_contract_dict_from_row(r) for r in ranked], "database"


def _contract_dict_from_cache_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": item.get("contract_type") or item.get("type"),
        "strike": item.get("strike_price") or item.get("strike"),
        "bid": item.get("bid"),
        "ask": item.get("ask"),
        "iv": item.get("implied_volatility") or item.get("impliedVolatility"),
        "delta": item.get("delta"),
        "gamma": item.get("gamma"),
        "theta": item.get("theta"),
        "vega": item.get("vega"),
        "oi": item.get("open_interest") or item.get("openInterest"),
        "volume": item.get("day_volume") or item.get("volume"),
    }


def _atm_options_from_chain_cache(sym: str, spot: float, *, limit: int = 6) -> tuple[list[dict[str, Any]], str]:
    cached = _as_dict(cache_get(key_options_chain(sym)))
    if not cached:
        return [], "miss"
    contracts_raw = cached.get("contracts")
    if not isinstance(contracts_raw, list) or not contracts_raw:
        return [], "miss"

    valid = [c for c in contracts_raw if isinstance(c, dict)]
    if spot > 0:
        valid.sort(
            key=lambda x: abs(float(x.get("strike_price") or x.get("strike") or 0) - spot)
        )
    return [_contract_dict_from_cache_item(c) for c in valid[:limit]], "redis"


def build_agent_context_from_cache(symbol: str) -> dict[str, object]:
    """Assemble agent market context from Redis and local DB only."""
    sym = symbol.strip().upper()
    ctx: dict[str, object] = {"symbol": sym}
    data_sources: dict[str, str] = {}

    overview, overview_src = _overview_from_cache(sym)
    if overview:
        ctx.update(overview)
        data_sources["overview"] = overview_src

    gex, gex_src = _gex_from_cache(sym)
    if gex:
        ctx["gex"] = gex
        data_sources["gex"] = gex_src

    news, news_src = _news_from_cache(sym)
    if news:
        ctx["recent_news"] = news
        data_sources["news"] = news_src

    analyst, analyst_src = _analyst_from_cache(sym)
    if analyst:
        ctx["recent_ratings"] = analyst.get("recent_ratings")
        if analyst.get("price_target_summary"):
            ctx["price_target"] = analyst.get("price_target_summary")
        data_sources["analyst_ratings"] = analyst_src

    spot = _spot_from_overview(overview) if overview else 0.0
    if spot <= 0 and gex and isinstance(gex.get("underlyingPrice"), (int, float)):
        spot = float(gex["underlyingPrice"])

    atm, atm_src = _atm_options_from_chain_cache(sym, spot)
    if not atm:
        atm, atm_src = _atm_options_from_db(sym, spot)
    if atm:
        ctx["atm_options"] = atm
        data_sources["atm_options"] = atm_src

    ctx["data_sources"] = data_sources
    if data_sources:
        ctx["cache_note"] = "数据来自平台缓存/数据库快照，非实时行情。"
    else:
        ctx["cache_note"] = "缓存未命中；回答时请说明缺失字段，勿编造数值。"
    return ctx
