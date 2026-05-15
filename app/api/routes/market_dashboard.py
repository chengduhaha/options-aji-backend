"""Dashboard aggregate endpoints."""

from __future__ import annotations

import json
import logging
import math
import time
from typing import Any, Optional

import httpx
import yfinance as yf
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.analytics.cboe_equity_pc import fetch_equity_pc_latest
from app.analytics.iv_metrics import vix_term_structure_hint
from app.analytics.market_hours import get_us_market_session
from app.api.deps import bearer_subscription_optional
from app.clients.fmp_client import get_fmp_client
from app.config import get_settings
from app.services.cache_service import (
    cache_get,
    cache_set,
    key_market_dashboard_overview,
)
from app.tools.openbb_tools import OpenBBToolkit, build_default_toolkit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/market", tags=["market"])

WATCHLIST_MOVER = ["SPY", "QQQ", "AAPL", "TSLA", "NVDA", "MSFT", "META", "AMZN"]
PULSE = ["SPY", "QQQ", "DIA", "IWM", "^VIX"]

_ai_summary_cache: dict[str, Any] = {"ts_monotonic": 0.0, "text": "", "model": ""}


class AiSummaryResponse(BaseModel):
    text: str
    generated_at_utc: str
    model: str
    cached: bool = False


@router.get("/overview")
def market_overview(
    _: Optional[str] = Depends(bearer_subscription_optional),
    refresh: bool = Query(False, description="为 true 时跳过 Redis，强制重新拉取并写回缓存"),
) -> dict[str, object]:
    """Single payload for OptionsAji home dashboard."""

    cfg = get_settings()
    cache_key = key_market_dashboard_overview()
    if not refresh and cfg.redis_enabled:
        hit = cache_get(cache_key)
        if isinstance(hit, dict):
            out = dict(hit)
            out["fromCache"] = True
            return out

    toolkit = build_default_toolkit()
    session, session_label = get_us_market_session()

    pulse_out: list[dict[str, object]] = []
    for sym in PULSE:
        qt = toolkit.get_quote(sym)
        sym_ui = "VIX" if sym == "^VIX" else sym
        invert = sym == "^VIX"
        last = qt.get("last_price")
        pct = qt.get("change_pct")
        pulse_out.append(
            {
                "symbol": sym_ui,
                "yahooSymbol": sym,
                "price": last,
                "changePct": pct,
                "invertColors": invert,
                "error": qt.get("error"),
            }
        )

    # VIX mini series (5 sessions) + levels (prefer pulse/FMP when yfinance empty)
    vix_series: list[float] = []
    vix_last: Optional[float] = None
    vix_chg: Optional[float] = None
    try:
        v = yf.Ticker("^VIX")
        h = v.history(period="7d", interval="1d")
        if h is not None and not h.empty and "Close" in h.columns:
            vix_series = [float(x) for x in h["Close"].dropna().tolist()[-5:]]
        qi = v.fast_info
        lp = qi.get("last_price")
        pc = qi.get("previous_close")
        if isinstance(lp, (int, float)):
            vix_last = float(lp)
        if (
            isinstance(lp, (int, float))
            and isinstance(pc, (int, float))
            and pc
            and not (isinstance(lp, float) and math.isnan(lp))
        ):
            vix_chg = float((float(lp) - float(pc)) / float(pc) * 100.0)
    except Exception as exc:
        logger.warning("vix mini series: %s", exc)

    if vix_last is None or vix_chg is None:
        for row in pulse_out:
            if row.get("symbol") == "VIX":
                px = row.get("price")
                if vix_last is None and isinstance(px, (int, float)):
                    vix_last = float(px)
                cp = row.get("changePct")
                if vix_chg is None and isinstance(cp, (int, float)):
                    vix_chg = float(cp)
                break

    band = "未知"
    if vix_last is not None:
        if vix_last < 15:
            band = "低波动(<15)"
        elif vix_last < 20:
            band = "正常(15-20)"
        elif vix_last < 30:
            band = "高波动(20-30)"
        else:
            band = "极端(>=30)"

    term = vix_term_structure_hint()

    pcrs: list[float] = []
    for sym in WATCHLIST_MOVER:
        bar = toolkit.frontend_market_bar(sym)
        p = bar.get("pcr")
        if isinstance(p, (int, float)):
            pcrs.append(float(p))
    pcr_mean = sum(pcrs) / len(pcrs) if pcrs else None

    cfg = get_settings()
    cboe_snap = fetch_equity_pc_latest(
        csv_url=cfg.cboe_equity_pc_csv_url,
        ttl_seconds=float(cfg.cboe_equity_pc_cache_seconds),
    )

    unusual = _scan_unusual_top(toolkit, limit=5)
    earnings = _upcoming_earnings(watchlist=WATCHLIST_MOVER, days_ahead=14)

    gex_quick: list[dict[str, object]] = []
    for sym in ("SPY", "QQQ"):
        g = toolkit.get_gex(sym)
        if isinstance(g, dict) and "netGex" in g:
            gex_quick.append(
                {
                    "symbol": sym,
                    "netGex": g.get("netGex"),
                    "gammaFlip": g.get("gammaFlip"),
                    "regime": g.get("regime"),
                }
            )

    import datetime as dt

    payload: dict[str, object] = {
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "marketSession": session,
        "marketSessionLabel": session_label,
        "pulse": pulse_out,
        "volatility": {
            "vix": vix_last,
            "vixChangePct": vix_chg,
            "band": band,
            "vixSeries": vix_series,
            "termStructure": term,
        },
        "liquidity": (
            {
                "putCallRatioEquityCboe": cboe_snap.put_call_ratio,
                "cboeAsOf": cboe_snap.trade_date,
                "cboeCallVolume": cboe_snap.call_volume,
                "cboePutVolume": cboe_snap.put_volume,
                "cboeTotalVolume": cboe_snap.total_volume,
                "source": "CBOE_EQUITY_PC_CSV",
                "sourceUrl": cboe_snap.source_url,
                "putCallRatioVolumeApprox": pcr_mean,
                "methodology": (
                    "CBOE published equity options P/C CSV (aggregate); "
                    "watchlist intraday P/C shown as putCallRatioVolumeApprox for context."
                ),
                "symbolsSampled": WATCHLIST_MOVER,
            }
            if cboe_snap is not None
            else {
                "putCallRatioVolumeApprox": pcr_mean,
                "methodology": (
                    "CBOE CSV unavailable; watchlist volume P/C mean from yfinance front expiry."
                ),
                "symbolsSampled": WATCHLIST_MOVER,
                "source": "YFINANCE_WATCHLIST_FALLBACK",
            }
        ),
        "unusual": unusual,
        "earnings": earnings,
        "gexQuick": gex_quick,
        "watchlist": WATCHLIST_MOVER,
        "fromCache": False,
    }

    if cfg.redis_enabled:
        cache_set(cache_key, payload, ttl=int(cfg.redis_cache_ttl_hot))

    return payload


@router.get("/{symbol}")
def market_symbol(
    symbol: str,
    _: Optional[str] = Depends(bearer_subscription_optional),
) -> dict[str, object]:
    """Single-symbol market snapshot for stock header usage."""
    sym = symbol.strip().upper()
    if not sym:
        return {"error": "empty_symbol"}
    if sym == "AI-SUMMARY":
        ai = market_ai_summary(_)
        return ai.model_dump()
    toolkit = build_default_toolkit()
    quote = toolkit.get_quote(sym)
    bar = toolkit.frontend_market_bar(sym)
    return {
        "symbol": sym,
        "quote": quote,
        "bar": bar,
    }


def _scan_unusual_top(toolkit: OpenBBToolkit, *, limit: int) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for sym in WATCHLIST_MOVER:
        ch = toolkit.get_option_chain_full(sym)
        if not isinstance(ch, dict) or ch.get("error"):
            continue
        exp = str(ch.get("expiration") or "")
        for side, key in (("call", "calls"), ("put", "puts")):
            arr = ch.get(key) or []
            if not isinstance(arr, list):
                continue
            for rec in arr:
                if not isinstance(rec, dict):
                    continue
                vol = float(rec.get("volume") or 0)
                oi = float(rec.get("openInterest") or 0)
                strike = rec.get("strike")
                if vol < 50 or oi < 1:
                    continue
                ratio = vol / max(oi, 1.0)
                rows.append(
                    {
                        "symbol": sym,
                        "type": side,
                        "strike": strike,
                        "expiration": exp,
                        "volume": vol,
                        "openInterest": oi,
                        "volOiRatio": round(ratio, 3),
                        "iv": rec.get("impliedVolatility"),
                    }
                )
    rows.sort(key=lambda r: float(r.get("volOiRatio") or 0), reverse=True)
    top = rows[:limit]
    for r in top:
        t = str(r.get("type") or "")
        r["sentiment"] = "Bullish" if t == "call" else "Bearish"
    return top


def _upcoming_earnings(*, watchlist: list[str], days_ahead: int) -> list[dict[str, object]]:
    import datetime as dt

    out: list[dict[str, object]] = []
    today = dt.datetime.now(dt.timezone.utc).date()
    horizon = today + dt.timedelta(days=days_ahead)
    sym_set = {s.strip().upper() for s in watchlist if s.strip()}
    cfg = get_settings()
    if cfg.fmp_api_key.strip():
        try:
            fmp = get_fmp_client()
            rows = fmp.get_earnings_calendar(today.isoformat(), horizon.isoformat())
            if isinstance(rows, list):
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    sym = str(row.get("symbol") or "").strip().upper()
                    if sym not in sym_set:
                        continue
                    raw = row.get("date")
                    if not isinstance(raw, str) or len(raw) < 10:
                        continue
                    try:
                        ed = dt.date.fromisoformat(raw[:10])
                    except ValueError:
                        continue
                    if today <= ed <= horizon:
                        out.append(
                            {
                                "symbol": sym,
                                "date": ed.isoformat(),
                                "note": "FMP earnings-calendar。",
                            }
                        )
            out.sort(key=lambda x: str(x.get("date")))
            return out[:12]
        except Exception as exc:
            logger.warning("FMP upcoming earnings: %s", exc)

    for sym in watchlist:
        try:
            t = yf.Ticker(sym)
            cal = getattr(t, "calendar", None)
            df = cal if cal is not None and hasattr(cal, "empty") else None
            ed = None
            if df is not None and not getattr(df, "empty", True) and "Earnings Date" in df.index:
                cell = df.loc["Earnings Date"]
                ts = getattr(cell, "iloc", lambda *_: cell)(0)
                if hasattr(ts, "date"):
                    ed = ts.date()
                elif isinstance(ts, dt.datetime):
                    ed = ts.date()
            if ed is None:
                eds = getattr(t, "earnings_dates", None)
                if eds is not None and hasattr(eds, "index") and len(getattr(eds, "index", [])) > 0:
                    idx = list(eds.index)
                    if idx:
                        ts0 = idx[0]
                        ed = ts0.date() if hasattr(ts0, "date") else None
            if ed is None:
                continue
            if today <= ed <= horizon:
                out.append(
                    {
                        "symbol": sym,
                        "date": ed.isoformat(),
                        "note": "EPS / expected move需后续接入完整财报端点。",
                    }
                )
        except Exception as exc:
            logger.debug("earnings %s: %s", sym, exc)
            continue
    out.sort(key=lambda x: str(x.get("date")))
    return out[:12]


@router.get("/ai-summary", response_model=AiSummaryResponse)
def market_ai_summary(
    _: Optional[str] = Depends(bearer_subscription_optional),
) -> AiSummaryResponse:
    cfg = get_settings()
    import datetime as dt

    now = time.monotonic()
    if _ai_summary_cache["text"] and now - float(_ai_summary_cache["ts_monotonic"]) < 3600:
        return AiSummaryResponse(
            text=str(_ai_summary_cache["text"]),
            generated_at_utc=str(_ai_summary_cache.get("generated_at_utc") or ""),
            model=str(_ai_summary_cache.get("model") or ""),
            cached=True,
        )

    api_key = cfg.openrouter_api_key.strip()
    if not api_key:
        payload = AiSummaryResponse(
            text="未配置 OPENROUTER_API_KEY，暂无法生成 AI 摘要。",
            generated_at_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
            model="",
            cached=False,
        )
        return payload

    overview = market_overview(_)
    payload = json.dumps(overview, ensure_ascii=False, default=str)
    prompt = (
        "你是 OptionsAji 市场编辑。请用中文输出 2-3 句话，概括当前指数强弱、波动率环境与 1-2 个简单风险点。"
        f"输入 JSON（可能较长，请抓重点）：{payload[:28000]}"
    )
    try:
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(
                f"{cfg.openrouter_base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": cfg.model_synthesis,
                    "messages": [
                        {"role": "system", "content": "仅输出紧凑中文短文，无 Markdown 标题。"},
                        {"role": "user", "content": prompt[:28000]},
                    ],
                    "temperature": 0.35,
                },
            )
            resp.raise_for_status()
            data: dict[str, object] = resp.json()
            choices = data.get("choices")
            text = ""
            if isinstance(choices, list) and choices:
                msg = choices[0].get("message") if isinstance(choices[0], dict) else None
                if isinstance(msg, dict) and isinstance(msg.get("content"), str):
                    text = str(msg["content"])
            if not text.strip():
                text = "模型未返回可用文本。"
    except Exception as exc:
        logger.warning("ai summary fail: %s", exc)
        text = f"AI 摘要生成失败：{type(exc).__name__}"

    gen_at = dt.datetime.now(dt.timezone.utc).isoformat()
    _ai_summary_cache.update(
        {
            "ts_monotonic": now,
            "text": text,
            "model": cfg.model_synthesis,
            "generated_at_utc": gen_at,
        }
    )
    return AiSummaryResponse(
        text=text,
        generated_at_utc=gen_at,
        model=cfg.model_synthesis,
        cached=False,
    )
