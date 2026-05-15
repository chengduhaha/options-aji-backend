"""4-Dimension Sentiment Dashboard — Analyst × Management × Social × News."""
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
from app.db.models import AnalystRatingRow, StockNewsRow, TickerSentimentSnapshotRow
from app.db.session import db_session_dep
from app.services.cache_service import cache_get, cache_set

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/sentiment", tags=["sentiment"])

_TTL = 3600  # 1 hour


def _analyst_sentiment(symbol: str, db: Session) -> dict:
    cutoff_date = (datetime.now(timezone.utc) - timedelta(days=90)).date()
    rows = db.execute(
        select(AnalystRatingRow)
        .where(
            and_(
                AnalystRatingRow.symbol == symbol,
                AnalystRatingRow.rating_date >= cutoff_date,
            )
        )
        .order_by(AnalystRatingRow.rating_date.desc())
        .limit(30)
    ).scalars().all()

    if not rows:
        cfg = get_settings()
        if cfg.fmp_api_key:
            try:
                pt = get_fmp_client().get_price_target_summary(symbol)
                if pt:
                    return {
                        "score": 65,
                        "label": "中性偏多",
                        "analyst_count": 0,
                        "target_price": pt.get("lastMonthAvgPriceTarget"),
                        "ratings": [],
                    }
            except Exception:
                pass
        return {"score": None, "label": "无数据", "analyst_count": 0, "ratings": []}

    def _is_buy(r: str) -> bool:
        r = (r or "").lower()
        return "buy" in r or "outperform" in r or "overweight" in r or "strong" in r

    def _is_sell(r: str) -> bool:
        r = (r or "").lower()
        return "sell" in r or "underperform" in r or "underweight" in r

    buy = sum(1 for row in rows if _is_buy(row.rating_to or ""))
    sell = sum(1 for row in rows if _is_sell(row.rating_to or ""))
    hold = len(rows) - buy - sell
    total = max(len(rows), 1)
    score = int((buy * 1.0 + hold * 0.5) / total * 100)

    return {
        "score": score,
        "label": "偏多" if score > 65 else "中性" if score > 40 else "偏空",
        "buy_count": buy,
        "hold_count": hold,
        "sell_count": sell,
        "analyst_count": len(rows),
        "ratings": [
            {
                "firm": r.analyst_company,
                "action": r.rating_action,
                "rating": r.rating_to,
                "price_target": r.price_target,
                "date": str(r.rating_date) if r.rating_date else None,
            }
            for r in rows[:5]
        ],
    }


def _mgmt_tone(symbol: str) -> dict:
    cfg = get_settings()
    if not cfg.fmp_api_key:
        return {"score": None, "note": "FMP未配置"}
    try:
        data = get_fmp_client()._get("/earnings-transcript", {"symbol": symbol, "limit": 1}) or []
        if not data:
            return {"score": None, "note": "无财报转录数据"}
        text = (data[0].get("content") or "")[:3000]
        if not text:
            return {"score": None, "note": "财报内容为空"}
        if not cfg.openrouter_api_key:
            pos_kws = ["record", "strong", "beat", "growth", "momentum", "solid", "accelerate"]
            neg_kws = ["headwinds", "uncertain", "slow", "miss", "decline", "challenge", "pressure"]
            pos = sum(text.lower().count(kw) for kw in pos_kws)
            neg = sum(text.lower().count(kw) for kw in neg_kws)
            total = max(pos + neg, 1)
            score = int(pos / total * 100)
            return {
                "score": score,
                "key_positive": pos_kws[:3],
                "key_negative": neg_kws[:2],
                "quarter": data[0].get("period"),
                "date": data[0].get("date"),
            }
        llm = ChatOpenAI(
            api_key=cfg.openrouter_api_key,
            base_url=cfg.openrouter_base_url,
            model=cfg.model_synthesis,
            temperature=0.1,
            timeout=20,
            max_retries=1,
        )
        import json
        prompt = (
            f"分析以下财报电话会议文本中管理层的语气倾向，\n"
            f"仅返回JSON：{{\"score\": 0-100, \"key_positive\": [\"词1\",\"词2\"], \"key_negative\": [\"词1\"]}}\n"
            f"score含义：100=极度乐观，50=中性，0=极度谨慎。\n文本：{text}"
        )
        out = llm.invoke([HumanMessage(content=prompt)])
        raw = str(getattr(out, "content", "{}") or "{}")
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1].lstrip("json").strip()
        parsed = json.loads(cleaned)
        return {
            "score": parsed.get("score"),
            "key_positive": parsed.get("key_positive", [])[:5],
            "key_negative": parsed.get("key_negative", [])[:5],
            "quarter": data[0].get("period"),
            "date": data[0].get("date"),
        }
    except Exception as exc:
        logger.debug("mgmt_tone %s: %s", symbol, exc)
        return {"score": None, "note": str(exc)[:80]}


def _social_sentiment(symbol: str, db: Session) -> dict:
    row = db.execute(
        select(TickerSentimentSnapshotRow)
        .where(TickerSentimentSnapshotRow.symbol == symbol)
        .order_by(TickerSentimentSnapshotRow.snapshot_time.desc())
        .limit(1)
    ).scalar_one_or_none()
    if not row:
        return {"score": None, "direction": "neutral", "note": "暂无社媒数据"}
    return {
        "score": row.sentiment_score,
        "direction": row.direction,
        "mention_count_24h": row.mention_count_24h,
        "mention_growth_pct": row.mention_growth_pct,
        "source_breakdown": row.source_breakdown,
        "snapshot_time": row.snapshot_time.isoformat(),
    }


def _news_sentiment(symbol: str, db: Session) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    rows = db.execute(
        select(StockNewsRow)
        .where(
            and_(
                StockNewsRow.published_at >= cutoff,
                StockNewsRow.symbols.contains([symbol]),
            )
        )
        .order_by(StockNewsRow.published_at.desc())
        .limit(15)
    ).scalars().all()

    if not rows:
        return {"score": None, "article_count": 0, "note": "无近期新闻"}

    pos_kws = ["上涨", "强劲", "超预期", "创新高", "利好", "突破", "增长",
               "beat", "strong", "record", "growth", "surge", "rally"]
    neg_kws = ["下跌", "疲软", "未达预期", "利空", "风险", "担忧",
               "weak", "miss", "decline", "risk", "cut", "warning"]
    pos = neg = 0
    for r in rows:
        text = ((r.summary_zh or "") + " " + (r.title_zh or "") + " " + (r.title or "")).lower()
        pos += sum(1 for kw in pos_kws if kw in text)
        neg += sum(1 for kw in neg_kws if kw in text)

    total = max(pos + neg, 1)
    score = int(pos / total * 100)
    return {
        "score": score,
        "article_count": len(rows),
        "positive_signals": pos,
        "negative_signals": neg,
        "latest_headlines": [r.title_zh or r.title for r in rows[:3]],
    }


def _strategy_suggestion(
    symbol: str,
    analyst: Optional[int],
    mgmt: Optional[int],
    social: Optional[int],
    news: Optional[int],
) -> str:
    cfg = get_settings()
    if not cfg.openrouter_api_key:
        return ""
    try:
        scores_str = (
            f"华尔街分析师：{analyst}/100  "
            f"管理层语气：{mgmt}/100  "
            f"散户情绪：{social}/100  "
            f"新闻情绪：{news}/100"
        )
        llm = ChatOpenAI(
            api_key=cfg.openrouter_api_key,
            base_url=cfg.openrouter_base_url,
            model=cfg.model_synthesis,
            temperature=0.3,
            timeout=20,
            max_retries=1,
        )
        prompt = (
            f"基于 {symbol} 的四维情绪数据，给出简洁期权策略建议（2-3句中文）：\n"
            f"{scores_str}\n"
            "指出当前情绪主要矛盾，建议最适合的期权策略方向，必须附免责声明。"
        )
        out = llm.invoke([HumanMessage(content=prompt)])
        return str(getattr(out, "content", "") or "").strip()
    except Exception as exc:
        logger.debug("strategy_suggestion %s: %s", symbol, exc)
        return ""


@router.get("/dashboard/{symbol}")
async def get_sentiment_dashboard(
    symbol: str,
    db: Session = Depends(db_session_dep),
    _: Optional[str] = Depends(bearer_subscription_optional),
):
    """Return 4-dimension sentiment dashboard for a stock."""
    sym = symbol.upper()
    cache_key = f"sentiment:dashboard:{sym}"
    if hit := cache_get(cache_key):
        return hit

    analyst_data, mgmt_data, social_data, news_data, strategy = await asyncio.gather(
        asyncio.to_thread(_analyst_sentiment, sym, db),
        asyncio.to_thread(_mgmt_tone, sym),
        asyncio.to_thread(_social_sentiment, sym, db),
        asyncio.to_thread(_news_sentiment, sym, db),
        asyncio.to_thread(
            _strategy_suggestion, sym,
            None, None, None, None  # will be filled after gather
        ),
        return_exceptions=True,
    )

    def safe(d: object, fb: dict) -> dict:
        return d if isinstance(d, dict) else fb

    analyst_data = safe(analyst_data, {"score": None})
    mgmt_data = safe(mgmt_data, {"score": None})
    social_data = safe(social_data, {"score": None})
    news_data = safe(news_data, {"score": None})

    scores = [
        s for s in [
            analyst_data.get("score"),
            mgmt_data.get("score"),
            social_data.get("score"),
            news_data.get("score"),
        ]
        if s is not None
    ]
    composite = int(sum(scores) / len(scores)) if scores else 50
    composite_label = (
        "极度乐观" if composite >= 80 else
        "偏乐观" if composite >= 60 else
        "中性" if composite >= 40 else
        "偏悲观"
    )

    ai_strategy = await asyncio.to_thread(
        _strategy_suggestion,
        sym,
        analyst_data.get("score"),
        mgmt_data.get("score"),
        social_data.get("score"),
        news_data.get("score"),
    )

    result = {
        "symbol": sym,
        "composite_score": composite,
        "composite_label": composite_label,
        "dimensions": {
            "analyst": {"label": "华尔街预期", **analyst_data},
            "management": {"label": "管理层语气", **mgmt_data},
            "social": {"label": "散户共识", **social_data},
            "news": {"label": "新闻情绪", **news_data},
        },
        "ai_strategy_zh": ai_strategy,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    cache_set(cache_key, result, ttl=_TTL)
    return result
