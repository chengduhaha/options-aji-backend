"""Retail FOMO vs Insider Divergence Scanner — identifies when retail FOMO-buys while insiders sell."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.api.deps import bearer_subscription_optional
from app.clients.fmp_client import get_fmp_client
from app.config import get_settings
from app.db.models import TickerSentimentSnapshotRow
from app.db.session import db_session_dep
from app.services.cache_service import cache_get, cache_set

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/divergence", tags=["divergence"])

_TTL = 900  # 15 min


def _hot_tickers(db: Session, min_growth: float, limit: int) -> list[dict]:
    """Get high-mention-growth tickers from sentiment snapshots in the last 24h."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    rows = db.execute(
        select(TickerSentimentSnapshotRow)
        .where(
            and_(
                TickerSentimentSnapshotRow.snapshot_time >= cutoff,
                TickerSentimentSnapshotRow.mention_growth_pct >= min_growth,
                TickerSentimentSnapshotRow.sentiment_score >= 55,
            )
        )
        .order_by(TickerSentimentSnapshotRow.mention_growth_pct.desc())
        .limit(limit)
    ).scalars().all()
    return [
        {
            "symbol": r.symbol,
            "sentiment_score": r.sentiment_score,
            "mention_count_24h": r.mention_count_24h,
            "growth_pct": float(r.mention_growth_pct or 0),
            "direction": r.direction,
            "snapshot_time": r.snapshot_time.isoformat(),
        }
        for r in rows
    ]


def _insider_sells(symbol: str, days: int) -> list[dict]:
    """Fetch Form 4 insider sell transactions from FMP."""
    cfg = get_settings()
    if not cfg.fmp_api_key:
        return []
    try:
        data = get_fmp_client()._get("/insider-trading", {"symbol": symbol, "limit": 30}) or []
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        sells = []
        for t in data:
            tx = str(t.get("transactionType") or t.get("type") or "")
            if "sale" not in tx.lower() and "sell" not in tx.lower() and "s-" not in tx.lower():
                continue
            raw_date = t.get("filingDate") or t.get("transactionDate") or ""
            try:
                d = datetime.fromisoformat(str(raw_date)[:10])
                if d.replace(tzinfo=timezone.utc) < cutoff:
                    continue
            except Exception:
                continue
            shares = float(t.get("securitiesTransacted") or t.get("sharesTransacted") or 0)
            price = float(t.get("price") or 0)
            sells.append(
                {
                    "reporter": str(t.get("reportingName") or "Unknown"),
                    "title": str(t.get("typeOfOwner") or "Executive"),
                    "shares": shares,
                    "price": price,
                    "total_value": shares * price,
                    "date": str(raw_date)[:10],
                }
            )
        return sells
    except Exception as exc:
        logger.debug("insider_sells %s: %s", symbol, exc)
        return []


def _ai_narrative(symbol: str, social: dict, insider: list[dict], score: int) -> str:
    """Generate concise Chinese narrative about the divergence signal."""
    cfg = get_settings()
    if not cfg.openrouter_api_key:
        return ""
    try:
        sell_total = sum(t.get("total_value", 0) for t in insider)
        sell_note = f"内部人士近期卖出约 ${sell_total:,.0f}" if insider else "近期无内部人士卖出记录"
        llm = ChatOpenAI(
            api_key=cfg.openrouter_api_key,
            base_url=cfg.openrouter_base_url,
            model=cfg.model_synthesis,
            temperature=0.3,
            timeout=20,
            max_retries=1,
        )
        prompt = (
            f"为 {symbol} 的散户与内部人背离情况写2-3句简短中文分析。\n"
            f"散户情绪 {social['sentiment_score']}/100，提及增速 +{social['growth_pct']:.0f}%。\n"
            f"{sell_note}。背离评分 {score}/100。\n"
            "给出简洁风险判断，并附一句免责声明。"
        )
        out = llm.invoke([HumanMessage(content=prompt)])
        return str(getattr(out, "content", "") or "").strip()
    except Exception as exc:
        logger.debug("narrative %s: %s", symbol, exc)
        return ""


@router.get("/scan")
async def scan_divergence(
    min_mention_growth: float = Query(80.0, ge=0),
    min_social_score: int = Query(55, ge=0, le=100),
    lookback_days: int = Query(5, ge=1, le=30),
    limit: int = Query(20, ge=1, le=50),
    db: Session = Depends(db_session_dep),
    _: Optional[str] = Depends(bearer_subscription_optional),
):
    """Scan for Retail FOMO vs Insider divergence signals."""
    cache_key = f"div:scan:{int(min_mention_growth)}:{min_social_score}:{lookback_days}"
    if hit := cache_get(cache_key):
        return hit

    tickers = _hot_tickers(db, min_growth=min_mention_growth, limit=limit * 2)

    if not tickers:
        return {
            "items": [],
            "scanned_at": datetime.now(timezone.utc).isoformat(),
            "note": "暂无社媒情绪数据，请确认社媒数据采集已启用（feature_social_enabled=true）。",
        }

    items: list[dict] = []
    tasks: list = []
    for td in tickers[:limit]:
        sym = td["symbol"]
        sells = _insider_sells(sym, days=lookback_days)
        sell_total = sum(t["total_value"] for t in sells)
        soc_score = td["sentiment_score"]
        intensity = min(sell_total / 100_000, 100.0)
        div_score = int(soc_score * 0.6 + intensity * 0.4)
        alert = "danger" if div_score > 80 else "warning" if div_score > 60 else "normal"
        rec = {
            "symbol": sym,
            "social_score": soc_score,
            "mention_count_24h": td["mention_count_24h"],
            "mention_growth_pct": round(td["growth_pct"], 1),
            "direction": td["direction"],
            "insider_sell_usd": int(sell_total),
            "insider_trade_count": len(sells),
            "divergence_score": div_score,
            "alert_level": alert,
            "insider_trades": sells[:3],
            "ai_narrative_zh": "",
        }
        items.append(rec)
        tasks.append(asyncio.to_thread(_ai_narrative, sym, td, sells, div_score))

    narratives = await asyncio.gather(*tasks, return_exceptions=True)
    for item, narr in zip(items, narratives):
        if isinstance(narr, str):
            item["ai_narrative_zh"] = narr

    items.sort(key=lambda x: x["divergence_score"], reverse=True)
    payload = {
        "items": items,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "total_scanned": len(tickers),
    }
    cache_set(cache_key, payload, ttl=_TTL)
    return payload


@router.get("/ticker/{symbol}")
async def ticker_divergence(
    symbol: str,
    lookback_days: int = Query(7, ge=1, le=30),
    db: Session = Depends(db_session_dep),
    _: Optional[str] = Depends(bearer_subscription_optional),
):
    """Divergence detail for a single ticker."""
    sym = symbol.upper()
    cache_key = f"div:ticker:{sym}:{lookback_days}"
    if hit := cache_get(cache_key):
        return hit

    row = db.execute(
        select(TickerSentimentSnapshotRow)
        .where(TickerSentimentSnapshotRow.symbol == sym)
        .order_by(TickerSentimentSnapshotRow.snapshot_time.desc())
        .limit(1)
    ).scalar_one_or_none()

    social = {
        "symbol": sym,
        "sentiment_score": row.sentiment_score if row else 50,
        "mention_count_24h": row.mention_count_24h if row else 0,
        "growth_pct": float(row.mention_growth_pct or 0) if row else 0.0,
        "direction": row.direction if row else "neutral",
    }

    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    history = db.execute(
        select(TickerSentimentSnapshotRow)
        .where(
            and_(
                TickerSentimentSnapshotRow.symbol == sym,
                TickerSentimentSnapshotRow.snapshot_time >= cutoff,
            )
        )
        .order_by(TickerSentimentSnapshotRow.snapshot_time.asc())
    ).scalars().all()

    sells = _insider_sells(sym, days=lookback_days)
    sell_total = sum(t["total_value"] for t in sells)
    intensity = min(sell_total / 100_000, 100.0)
    div_score = int(social["sentiment_score"] * 0.6 + intensity * 0.4)
    alert = "danger" if div_score > 80 else "warning" if div_score > 60 else "normal"

    narrative = await asyncio.to_thread(_ai_narrative, sym, social, sells, div_score)

    result = {
        "symbol": sym,
        "divergence_score": div_score,
        "alert_level": alert,
        "social": social,
        "insider_trades": sells,
        "insider_sell_total_usd": int(sell_total),
        "ai_narrative_zh": narrative,
        "social_history": [
            {
                "time": r.snapshot_time.isoformat(),
                "sentiment_score": r.sentiment_score,
                "mention_count": r.mention_count_24h,
            }
            for r in history
        ],
    }
    cache_set(cache_key, result, ttl=300)
    return result
