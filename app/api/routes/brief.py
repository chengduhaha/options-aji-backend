"""Market brief — daily AI-generated market summary."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from langchain_core.messages import HumanMessage, SystemMessage

from app.api.deps import bearer_subscription_optional
from app.clients.fmp_client import get_fmp_client
from app.config import get_settings
from app.db.session import SessionLocal
from app.db.models import StockNewsRow
from app.services.cache_service import TTL_AI, cache_get, cache_set, key_ai_market_summary
from app.services.locale import Locale, parse_locale
from app.services.llm_router import build_chat_openai, has_llm_provider
from app.analytics.gex_compute import compute_gex_profile
from sqlalchemy import select, desc

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agent", tags=["agent"])


def _fetch_market_data() -> dict[str, object]:
    """Gather all market data needed for a daily brief."""
    ctx: dict[str, object] = {}
    cfg = get_settings()
    fmp = get_fmp_client() if cfg.fmp_api_key else None

    # 1. Indices pulse
    if fmp:
        try:
            indices = fmp.get_all_index_quotes()
            pulse = {}
            for idx in indices:
                sym = idx.get("symbol", "")
                if sym in ("^GSPC", "^IXIC", "^DJI", "^VIX"):
                    pulse[sym] = {
                        "price": idx.get("price"),
                        "change_pct": idx.get("changePercentage"),
                    }
            ctx["indices"] = pulse
        except Exception:
            pass

    # 2. GEX for SPY/QQQ
    for sym in ("SPY", "QQQ"):
        try:
            gex = compute_gex_profile(sym)
            if isinstance(gex, dict) and not gex.get("error"):
                ctx[f"gex_{sym}"] = {
                    "netGex_bn": gex.get("netGex"),
                    "regime": gex.get("regime"),
                    "gammaFlip": gex.get("gammaFlip"),
                    "maxPain": gex.get("maxPain"),
                }
        except Exception:
            pass

    # 3. Macro events today
    if fmp:
        try:
            today = datetime.now(timezone.utc)
            events = fmp.get_economic_calendar(
                today.strftime("%Y-%m-%d"),
                today.strftime("%Y-%m-%d"),
            )
            ctx["macro_events"] = [
                {"event": e.get("event"), "impact": e.get("impact"), "time": str(e.get("date", ""))[11:16]}
                for e in (events or [])[:8]
            ]
        except Exception:
            pass

    # 4. Top news
    session = SessionLocal()
    try:
        rows = session.execute(
            select(StockNewsRow).order_by(desc(StockNewsRow.published_at)).limit(5)
        ).scalars().all()
        if rows:
            ctx["top_news"] = [
                {"title": r.title_zh or r.title, "source": r.source}
                for r in rows
            ]
    except Exception:
        pass
    finally:
        session.close()

    # 5. Gainers / losers snapshot
    if fmp:
        try:
            gainers = fmp.get_gainers()[:3]
            losers = fmp.get_losers()[:3]
            ctx["gainers"] = [{"symbol": g.get("symbol"), "change": g.get("changesPercentage")} for g in gainers]
            ctx["losers"] = [{"symbol": l.get("symbol"), "change": l.get("changesPercentage")} for l in losers]
        except Exception:
            pass

    return ctx


def _generate_brief_text(data: dict[str, object], locale: Locale = "zh") -> str:
    """Call LLM to synthesize a market brief."""
    cfg = get_settings()
    if not has_llm_provider(cfg):
        return (
            "LLM provider is not configured; market brief unavailable."
            if locale == "en"
            else "服务端未配置 LLM Provider，无法生成市场简报。"
        )

    llm = build_chat_openai(
        cfg,
        openrouter_model=cfg.model_synthesis,
        source="agent_brief",
        temperature=0.3,
        timeout=120,
        max_retries=1,
    )

    lang = "English" if locale == "en" else "中文"
    system = (
        f"你是美股期权与市场结构分析师 OptionsAji。用{lang}生成每日市场简报。\n"
        "格式要求：\n"
        "━━━ 大盘环境 ━━━━━\n"
        "SPY $xxx (x.xx%) | VIX x.xx | P/C x.xx\n\n"
        "━━━ GEX 快览 ━━━━━\n"
        "SPY: 正/负Gamma · Net $XB · Flip at $xxx\n\n"
        "━━━ 今日宏观 ━━━━━\n"
        "时间 · 事件 · 影响力\n\n"
        "━━━ 异动追踪 ━━━━━\n"
        "标的 | 信号 | 说明\n\n"
        "━━━ AI 策略提示 ━━━━━\n"
        "基于当前市场环境给出方向性建议\n"
        "不自造成交价。所有数据仅供参考，不构成投资建议。"
    )

    human = HumanMessage(content=json.dumps(data, ensure_ascii=False, default=str))

    try:
        out = llm.invoke([SystemMessage(content=system), human])
        text = getattr(out, "content", None)
        return "" if text is None else str(text)
    except Exception as exc:
        logger.exception("Brief generation failed: %s", exc)
        return (
            "Failed to generate market brief. Please try again later."
            if locale == "en"
            else "生成市场简报失败，请稍后重试。"
        )


@router.get("/brief")
def get_market_brief(
    locale: str = Query(default="zh", pattern="^(zh|en)$"),
    _=Depends(bearer_subscription_optional),
) -> dict:
    """Return cached or freshly generated market brief."""
    loc = parse_locale(locale)
    cache_key = key_ai_market_summary(loc)
    cached = cache_get(cache_key)
    if cached and isinstance(cached, dict) and cached.get("brief"):
        return cached

    data = _fetch_market_data()
    brief = _generate_brief_text(data, loc)
    result = {
        "brief": brief,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_snapshot": data,
    }
    cache_set(cache_key, result, ttl=TTL_AI)
    return result
