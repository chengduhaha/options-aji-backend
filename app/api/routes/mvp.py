"""MVP war-room aggregation routes."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps_access_key import MvpEntitlement, resolve_mvp_entitlement
from app.services.mvp_entitlement import (
    mvp_envelope,
    redact_macro_calendar,
    redact_market_insights,
    redact_playbook_hints,
    redact_stock_insights,
    redact_war_room,
)
from app.config import get_settings
from app.api.routes.macro import get_macro_calendar
from app.db.models import TreasuryRateRow
from app.db.session import db_session_dep
from app.ingest.message_store import list_discord_feed_rows
from app.services.cache_service import TTL_AI, cache_get, cache_set
from app.services.llm_router import has_llm_provider, post_chat_completions_with_fallback
from app.services.mvp_market_agent import (
    MvpMarketInsightsPayload,
    _rule_based_insights,
    generate_mvp_market_insights,
    get_cached_mvp_market_insights,
    market_insights_cache_key,
)
from app.services.mvp_market_context import build_mvp_market_context
from app.services.discord_menu_authors import resolve_author_filter
from app.services.locale import Locale, parse_locale, pick_text
from app.services.mvp_stock_options_agent import (
    StockOptionsInsightRequest,
    StockOptionsInsightsPayload,
    generate_fast_stock_options_insights,
    generate_stock_options_insights,
    get_cached_stock_options_insights,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/mvp", tags=["mvp"])
_llm_background_scheduled_at: dict[str, float] = {}


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _should_schedule_llm(cache_key: str, cooldown_seconds: int = 60) -> bool:
    now = time.monotonic()
    last = _llm_background_scheduled_at.get(cache_key)
    if last is not None and now - last < cooldown_seconds:
        return False
    _llm_background_scheduled_at[cache_key] = now
    return True


def _log_future_exception(task: Any) -> None:
    try:
        exc = task.exception()
    except Exception as exc:
        logger.warning("MVP background LLM warm status failed: %s", exc)
        return
    if exc is not None:
        logger.warning("MVP background LLM warm failed: %s", exc)


def _warm_stock_options_insights_blocking(body: StockOptionsInsightRequest) -> None:
    asyncio.run(generate_stock_options_insights(body))


def _warm_market_insights_blocking(context: dict[str, Any], locale: Locale) -> None:
    asyncio.run(generate_mvp_market_insights(context, locale=locale))


def _treasury_read(row: TreasuryRateRow | None) -> dict[str, Any]:
    if row is None:
        return {
            "label": "等待利率数据",
            "summary_zh": "国债曲线缺少关键期限，暂时只把图表作为利率水平参考。",
            "spreads": {},
        }
    month1 = _num(row.month1)
    year2 = _num(row.year2)
    year10 = _num(row.year10)
    year30 = _num(row.year30)
    spread_10y_2y = year10 - year2 if year10 is not None and year2 is not None else None
    spread_30y_10y = year30 - year10 if year30 is not None and year10 is not None else None
    spread_10y_1m = year10 - month1 if year10 is not None and month1 is not None else None

    if spread_10y_2y is None:
        label = "等待利率数据"
        summary = "缺少 2Y 或 10Y 数据，暂时无法判断倒挂和陡峭化。"
    elif spread_10y_2y < -0.25:
        label = "曲线深度倒挂"
        summary = "2Y 明显高于 10Y，市场仍在交易降息和增长放缓预期；成长股反弹更依赖利率回落和风险偏好修复。"
    elif spread_10y_2y < 0:
        label = "曲线轻度倒挂"
        summary = "短端仍高于长端，但倒挂不深；盘中重点看 10Y 是否继续上行，长端上行会压制 QQQ/NVDA 这类久期资产。"
    elif spread_30y_10y is not None and spread_30y_10y > 0.25:
        label = "长端偏陡"
        summary = "30Y 相对 10Y 偏高，长端期限溢价抬升；如果同时伴随美元走强，成长股追高需要降低仓位。"
    else:
        label = "曲线相对正常"
        summary = "2Y/10Y 未明显倒挂，利率曲线对风险资产的压制相对有限，盘中更应关注指数和波动率确认。"

    return {
        "label": label,
        "summary_zh": summary,
        "as_of": str(row.rate_date),
        "spreads": {
            "10y_2y": spread_10y_2y,
            "30y_10y": spread_30y_10y,
            "10y_1m": spread_10y_1m,
        },
    }


WAR_ROOM_IMPACTS = frozenset({"利好", "利空", "中性", "风险"})
WAR_ROOM_SCOPES = frozenset(
    {
        "equity_broad",
        "oil_energy",
        "rates_bonds",
        "single_stock",
        "macro_geo",
        "mixed",
    }
)
_SCOPE_ALIASES: dict[str, str] = {
    "equity": "equity_broad",
    "broad": "equity_broad",
    "index": "equity_broad",
    "oil": "oil_energy",
    "energy": "oil_energy",
    "crude": "oil_energy",
    "rates": "rates_bonds",
    "bond": "rates_bonds",
    "bonds": "rates_bonds",
    "treasury": "rates_bonds",
    "stock": "single_stock",
    "macro": "macro_geo",
    "geopolitical": "macro_geo",
    "geo": "macro_geo",
    "divergent": "mixed",
    "multi": "mixed",
}


def _normalize_war_room_event(raw: dict[str, Any]) -> dict[str, Any]:
    """Sanitize LLM / fallback event payloads for MVP war-room."""
    ev = dict(raw)
    impact = str(ev.get("impact") or "中性").strip()
    if impact not in WAR_ROOM_IMPACTS:
        impact = "中性"
    ev["impact"] = impact

    scope_raw = str(ev.get("impact_scope") or "equity_broad").strip().lower()
    scope = _SCOPE_ALIASES.get(scope_raw, scope_raw)
    if scope not in WAR_ROOM_SCOPES:
        scope = "equity_broad"
    ev["impact_scope"] = scope

    assets_raw = ev.get("related_assets")
    if not isinstance(assets_raw, list):
        assets_raw = ev.get("tickers") if isinstance(ev.get("tickers"), list) else []
    ev["related_assets"] = [
        str(a).strip().upper() for a in assets_raw if str(a).strip()
    ][:8]

    note = str(ev.get("impact_note_zh") or "").strip()
    ev["impact_note_zh"] = note[:120] if note else ""
    watch = str(ev.get("watch_zh") or "").strip()
    ev["watch_zh"] = watch[:200] if watch else ""
    for key, limit in (
        ("deep_dive_zh", 420),
        ("trade_implications_zh", 360),
        ("scenario_zh", 360),
        ("risk_watch_zh", 300),
    ):
        value = str(ev.get(key) or "").strip()
        ev[key] = value[:limit] if value else ""
    return ev


def _fallback_discord_event_analysis(title: str, body: str, tickers: list[str]) -> dict[str, Any]:
    """Rule-based event interpretation used when the war-room LLM is unavailable."""
    lower = f"{title} {body} {' '.join(tickers)}".lower()
    related_assets = list(dict.fromkeys(tickers))
    impact = "中性"
    scope = "equity_broad"
    impact_note = "该消息需要结合 SPY/QQQ、VIX 和成交量确认，单条消息不直接等同交易方向。"
    watch = "开盘后先看 SPY/QQQ 是否同向放量，再确认消息是否被市场定价。"
    deep = "这条消息被纳入关键事件，是因为它可能改变当天风险偏好、利率预期或板块资金流，而不是只提供新闻标题。"
    trade = "正股先等待价格靠近关键区间或突破确认；期权只在流动性、DTE 和价差可接受时表达方向。"
    scenario = "若指数与相关资产同向确认，可顺势观察；若消息方向与价格背离，优先降低仓位或等待二次确认。"
    risk = "若 VIX 抬升、价差扩大或开盘 15 分钟内快速反转，放弃追价，避免裸买高溢价期权。"

    if any(key in lower for key in ("fed", "fomc", "rate", "rates", "yield", "treasury", "inflation", "cpi", "ppi", "pce", "jobless", "payroll")):
        scope = "rates_bonds"
        impact = "风险"
        related_assets = list(dict.fromkeys(["SPY", "QQQ", "TLT", *related_assets]))
        impact_note = "利率或通胀预期变化会影响成长股估值和期权波动率。"
        watch = "重点盯 10Y、DXY、VIX 与 QQQ 是否同向反应。"
        deep = "这类消息通过美联储路径和折现率影响大盘，不应只看标题利好利空。"
        trade = "若 10Y 上行且 QQQ 转弱，减少追多 call；若利率回落并放量修复，再考虑更小仓位跟随。"
        scenario = "利率下行 + QQQ 放量站稳偏风险偏好修复；利率上行 + VIX 抬升则偏防守。"
        risk = "若收益率和美元同时走强，成长股多头假设失效，期权买方要控制权利金。"
    elif any(key in lower for key in ("wti", "crude", "oil", "opec", "energy")):
        scope = "oil_energy"
        impact = "风险"
        related_assets = list(dict.fromkeys(["USO", "XLE", "SPY", *related_assets]))
        impact_note = "油价大波动会影响能源链、通胀预期和风险偏好，方向需看传导。"
        watch = "观察 USO/XLE 与 10Y 是否同步上行，以及 SPY 是否受通胀预期压制。"
        deep = "原油消息不是单纯商品涨跌，它可能通过通胀预期和能源板块权重影响大盘。"
        trade = "能源股可看相对强弱；指数期权不宜只因油价单边波动直接下注。"
        scenario = "油价上行但能源股强、指数稳定，影响偏分化；油价上行叠加利率上行，指数压力更大。"
        risk = "若油价冲高回落或相关 ETF 不跟，说明消息可能已被定价，避免追入。"
    elif any(key in lower for key in ("nvda", "nvidia", "earnings", "q1", "q2", "results")):
        scope = "single_stock"
        impact = "利空" if any(key in lower for key in ("down", "跌", "lower", "miss")) else "中性"
        related_assets = list(dict.fromkeys(["NVDA", "QQQ", *related_assets]))
        impact_note = "大型权重股消息会通过 QQQ 和半导体链传导到指数风险偏好。"
        watch = "观察 NVDA 开盘后是否带动 SOXX/QQQ 同向，避免只看盘前跳动。"
        deep = "权重股财报会影响指数权重、AI 交易主线和期权隐含波动率。"
        trade = "正股等开盘量价确认；期权优先比较 IV 是否已过高，必要时用价差替代裸买。"
        scenario = "若 NVDA 快速收复盘前跌幅，科技风险偏好可能修复；若跌幅扩大并拖累 QQQ，转防守。"
        risk = "若 IV 快速回落或买卖价差扩大，短期期权即使方向正确也可能不划算。"
    elif any(key in lower for key in ("opens lower", "market opens", "s&p", "nasdaq", "spx", "spy", "qqq")):
        scope = "equity_broad"
        impact = "风险" if any(key in lower for key in ("lower", "跌", "down")) else "中性"
        related_assets = list(dict.fromkeys(["SPY", "QQQ", *related_assets]))
        impact_note = "指数开盘方向是盘中风险偏好的第一层确认，但仍需看延续性。"
        watch = "等开盘 15-30 分钟确认高低点和成交量，不用第一根大波动追单。"
        deep = "开盘方向本身不是事件，但它能验证盘前消息是否真正进入定价。"
        trade = "若 SPY/QQQ 同向放量，可按方向找强弱个股；若快速反抽或分化，先缩小观察名单。"
        scenario = "放量延续代表消息被交易；快速反转代表盘前叙事可能失效。"
        risk = "若指数与 VIX 同涨或板块分化严重，方向信号质量下降。"

    return {
        "impact": impact,
        "impact_scope": scope,
        "related_assets": related_assets[:8],
        "impact_note_zh": impact_note,
        "watch_zh": watch,
        "deep_dive_zh": deep,
        "trade_implications_zh": trade,
        "scenario_zh": scenario,
        "risk_watch_zh": risk,
        "impact_note_en": (
            "Cross-check SPY/QQQ, VIX, and volume before treating this headline as directional."
        ),
        "watch_en": "After the open, watch whether SPY/QQQ confirm with volume before acting.",
        "deep_dive_en": (
            "This matters because it can shift risk appetite, rates, or sector flows — not just headlines."
        ),
        "trade_implications_en": (
            "Wait for price near key levels or a confirmed break; use options only with liquid spreads."
        ),
        "scenario_en": (
            "If index and related assets confirm, lean with the move; if price diverges, reduce size."
        ),
        "risk_watch_en": (
            "If VIX rises, spreads widen, or the first 15 minutes reverse, avoid chasing premium."
        ),
    }


MACRO_EVENT_ZH: tuple[tuple[str, str], ...] = (
    ("nonfarm", "非农就业"),
    ("payrolls", "非农就业"),
    ("initial jobless", "初请失业金"),
    ("jobless", "初请失业金"),
    ("unemployment", "失业率"),
    ("cpi", "CPI 通胀"),
    ("ppi", "PPI 生产者物价"),
    ("pce", "PCE 通胀"),
    ("fomc", "FOMC 利率决议"),
    ("fed", "美联储"),
    ("gdp", "GDP"),
    ("pmi", "PMI"),
    ("retail sales", "零售销售"),
    ("consumer confidence", "消费者信心"),
    ("philadelphia fed", "费城联储制造业"),
    ("philly fed", "费城联储制造业"),
    ("ism", "ISM 指数"),
    ("housing", "房地产数据"),
    ("building permits", "营建许可"),
    ("home sales", "房地产销售"),
    ("mortgage", "抵押贷款利率"),
    ("tips auction", "TIPS 国债拍卖"),
    ("bill auction", "短债拍卖"),
    ("natural gas", "天然气库存"),
    ("trade balance", "贸易帐"),
    ("jolts", "JOLTS 职位空缺"),
)


def _macro_event_title_zh(event_name: str) -> str:
    raw = str(event_name or "").strip()
    lower = raw.lower()
    for key, zh in MACRO_EVENT_ZH:
        if key in lower:
            return zh
    if any("\u4e00" <= ch <= "\u9fff" for ch in raw):
        return raw
    return f"宏观：{raw}" if raw else "宏观事件"


def _format_macro_value(value: Any) -> str:
    if value in (None, ""):
        return "未公布"
    return str(value)


def _impact_zh(impact: Any) -> str:
    raw = str(impact or "").strip().lower()
    if raw == "high":
        return "高影响"
    if raw == "medium":
        return "中影响"
    if raw == "low":
        return "低影响"
    return str(impact or "未标注")


def _fallback_macro_calendar_insights(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Chinese, actionable fallback when the macro calendar LLM is unavailable."""
    normalized: list[dict[str, Any]] = []
    high_count = 0
    medium_count = 0
    for ev in events[:50]:
        impact = str(ev.get("impact") or "")
        if impact == "High":
            high_count += 1
        elif impact == "Medium":
            medium_count += 1
        event_name = str(ev.get("event") or ev.get("event_name") or "")
        title_zh = _macro_event_title_zh(event_name)
        estimate = _format_macro_value(ev.get("estimate"))
        previous = _format_macro_value(ev.get("previous"))
        actual = _format_macro_value(ev.get("actual"))
        impact_text = _impact_zh(impact)
        why = (
            f"{title_zh}是{impact_text}数据，预期 {estimate}，前值 {previous}，实际 {actual}。"
            "公布前先看预期差，公布后重点看美债收益率、美元和指数期货是否同向反应。"
        )
        if "通胀" in title_zh or title_zh in {"PCE 通胀", "PPI 生产者物价"}:
            trading = "通胀高于预期通常推升利率压力，成长股和长久期期权追多需要更谨慎。"
        elif title_zh in {"初请失业金", "非农就业", "失业率", "JOLTS 职位空缺"}:
            trading = "就业数据影响降息预期，强于预期可能压制降息交易，弱于预期则需区分增长担忧和利率利好。"
        elif "美联储" in title_zh or "FOMC" in title_zh:
            trading = "美联储相关事件会直接影响利率路径，先降低开盘前方向仓位，等声明和问答后的二次反应。"
        else:
            trading = "该事件主要通过利率、美元和风险偏好传导到 SPY/QQQ，单独标题不能直接等同交易方向。"
        normalized.append(
            {
                "date": ev.get("date"),
                "country": ev.get("country"),
                "event": event_name,
                "title_zh": title_zh,
                "impact": impact,
                "impact_zh": impact_text,
                "estimate": ev.get("estimate"),
                "previous": ev.get("previous"),
                "actual": ev.get("actual"),
                "why_it_matters_zh": why,
                "trading_impact_zh": trading,
                "watch_zh": "公布后 5-15 分钟观察 10Y、DXY、SPY/QQQ 是否同向确认，避免只看第一个跳动。",
            }
        )

    if high_count:
        market_read = f"当天有 {high_count} 个高影响宏观事件，开盘前不宜只按技术位下单，要先等数据落地后的利率和指数确认。"
    elif medium_count:
        market_read = f"当天以 {medium_count} 个中影响事件为主，宏观风险不低但通常需要结合盘中价格反应确认。"
    else:
        market_read = "当天宏观日历冲击较低，交易重心更可能回到财报、新闻、资金流和期权结构。"

    return {
        "engine": "rules",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "market_read_zh": market_read,
        "watch_plan": [
            "盘前先标出高影响数据发布时间，数据前减少追价和裸买期权。",
            "数据公布后看 10Y、DXY、VIX 与 SPY/QQQ 是否同向确认。",
            "若价格先冲后回落，优先等待 15 分钟级别确认再选择正股或期权结构。",
        ],
        "events": normalized,
    }


def _dedupe_macro_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for ev in events:
        key = (
            str(ev.get("date") or ""),
            str(ev.get("country") or ""),
            str(ev.get("event") or ev.get("event_name") or "").lower(),
            str(ev.get("impact") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(ev)
    return deduped


def _macro_insights_cache_key(from_date: str, to_date: str, country: str) -> str:
    raw = json.dumps(
        {
            "from": from_date,
            "to": to_date,
            "country": country,
            "bucket": _five_minute_bucket_utc(),
        },
        sort_keys=True,
    )
    return f"mvp:macro-calendar-insights:v1:{hash(raw)}"


def _get_cached_macro_calendar_llm(from_date: str, to_date: str, country: str) -> dict[str, Any] | None:
    cached = cache_get(_macro_insights_cache_key(from_date, to_date, country))
    return cached if isinstance(cached, dict) else None


def _normalize_macro_ai_payload(ai: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    fallback_events = fallback.get("events") if isinstance(fallback.get("events"), list) else []
    raw_events = ai.get("events") if isinstance(ai.get("events"), list) else []
    events: list[dict[str, Any]] = []
    for idx, base in enumerate(fallback_events[:50]):
        raw = raw_events[idx] if idx < len(raw_events) and isinstance(raw_events[idx], dict) else {}
        merged = dict(base)
        for key in ("title_zh", "why_it_matters_zh", "trading_impact_zh", "watch_zh"):
            value = str(raw.get(key) or "").strip()
            if value:
                merged[key] = value[:520]
        events.append(merged)
    market_read = str(ai.get("market_read_zh") or "").strip() or str(fallback.get("market_read_zh") or "")
    raw_plan = ai.get("watch_plan") if isinstance(ai.get("watch_plan"), list) else []
    plan = [str(x).strip()[:180] for x in raw_plan if str(x).strip()][:5]
    if not plan:
        plan = list(fallback.get("watch_plan") or [])
    return {
        "engine": "llm",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "market_read_zh": market_read[:700],
        "watch_plan": plan,
        "events": events,
    }


def _call_macro_calendar_llm(
    events: list[dict[str, Any]],
    fallback: dict[str, Any],
    from_date: str,
    to_date: str,
    country: str,
) -> dict[str, Any] | None:
    cfg = get_settings()
    if not has_llm_provider(cfg) or not events:
        return None
    cache_key = _macro_insights_cache_key(from_date, to_date, country)
    cached = _get_cached_macro_calendar_llm(from_date, to_date, country)
    if cached:
        return cached
    system = (
        "你是华语美股宏观交易教练。把英文经济日历翻译并解释为散户能执行的盘前观察计划。"
        "只输出 JSON：{\"market_read_zh\":\"\","
        "\"watch_plan\":[\"...\"],"
        "\"events\":[{\"title_zh\":\"\",\"why_it_matters_zh\":\"\","
        "\"trading_impact_zh\":\"\",\"watch_zh\":\"\"}]}。"
        "不要承诺收益，不要编造实际值；若 actual 为空，说明等待公布。"
        "解释必须连接到 SPY/QQQ、美债收益率、美元、VIX 和期权波动率。"
        "events 顺序必须与输入一致，每条中文不超过 120 字。"
    )
    prompt = {
        "from_date": from_date,
        "to_date": to_date,
        "country": country,
        "events": events[:50],
        "rule_fallback": fallback,
    }
    payload = {
        "temperature": 0.2,
        "max_tokens": 2200,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)[:14000]},
        ],
    }
    try:
        data, _provider = post_chat_completions_with_fallback(
            payload,
            cfg=cfg,
            source="mvp_macro_calendar",
            timeout=90.0,
        )
        content = data["choices"][0]["message"].get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("empty LLM content")
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            start = content.find("{")
            end = content.rfind("}")
            if start < 0 or end <= start:
                raise
            parsed = json.loads(content[start : end + 1])
        if isinstance(parsed, dict):
            normalized = _normalize_macro_ai_payload(parsed, fallback)
            cache_set(cache_key, normalized, ttl=TTL_AI)
            return normalized
    except Exception as exc:
        logger.warning("MVP macro calendar LLM failed: %s", exc)
    return None


def _event_from_discord(row: Any) -> dict[str, Any]:
    title = (row.enrichment_title_zh or "").strip() or row.content.strip().splitlines()[0][:80]
    body = (row.enrichment_summary_zh or "").strip()
    if not body:
        bullets = [b for b in row.enrichment_bullets_zh if str(b).strip()]
        body = " ".join(str(b) for b in bullets[:3])
    if not body:
        body = row.content.strip()[:220]
    tickers = [str(t).strip().upper() for t in (row.tickers or []) if str(t).strip()]
    fallback_analysis = _fallback_discord_event_analysis(title, body, tickers)
    return _normalize_war_room_event(
        {
            "id": row.id,
            "title": title,
            "body": body,
            "tag": "Discord",
            "impact": fallback_analysis["impact"],
            "impact_scope": fallback_analysis["impact_scope"],
            "time": row.timestamp_utc_iso,
            "tickers": tickers,
            "related_assets": fallback_analysis["related_assets"],
            "impact_score": 50,
            "impact_note_zh": fallback_analysis["impact_note_zh"],
            "watch_zh": fallback_analysis["watch_zh"],
            "deep_dive_zh": fallback_analysis["deep_dive_zh"],
            "trade_implications_zh": fallback_analysis["trade_implications_zh"],
            "scenario_zh": fallback_analysis["scenario_zh"],
            "risk_watch_zh": fallback_analysis["risk_watch_zh"],
        }
    )


def _fallback_plan(events: list[dict[str, Any]], treasury: dict[str, Any]) -> list[str]:
    plan: list[str] = []
    if events:
        plan.append(f"盘前主线先围绕「{events[0]['title']}」做情景推演，相关标的等待开盘后量价确认。")
    else:
        plan.append("盘前没有足够高置信事件，先以指数、VIX 和期权结构判断今天风险偏好。")
    plan.append("开盘后等待 15-30 分钟确认 SPY/QQQ 是否同向放量，再决定是否进入个股。")
    plan.append("期权只筛成交量、OI 和买卖价差都可接受的合约，不用低流动性深虚值表达方向。")
    plan.append(str(treasury.get("summary_zh") or "国债曲线暂时只作为背景变量。"))
    return plan


def _five_minute_bucket_utc() -> str:
    """Align LLM cache expiry with MVP front-end refresh (every 5 minutes)."""
    now = datetime.now(timezone.utc)
    bucket_min = (now.minute // 5) * 5
    bucket = now.replace(minute=bucket_min, second=0, microsecond=0)
    return bucket.isoformat()


def _llm_cache_key(
    events: list[dict[str, Any]],
    treasury: dict[str, Any],
    hours: int,
    *,
    authors: Optional[list[str]] = None,
    locale: Locale = "zh",
) -> str:
    raw = json.dumps(
        {
            "events": events[:20],
            "treasury": treasury,
            "hours": hours,
            "authors": authors or [],
            "locale": locale,
            "bucket": _five_minute_bucket_utc(),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return f"mvp:war-room:v5:{hash(raw)}"


def _get_cached_war_room_llm(
    events: list[dict[str, Any]],
    treasury: dict[str, Any],
    hours: int,
    *,
    authors: Optional[list[str]] = None,
    locale: Locale = "zh",
) -> dict[str, Any] | None:
    cached = cache_get(_llm_cache_key(events, treasury, hours, authors=authors, locale=locale))
    return cached if isinstance(cached, dict) else None


def _localize_war_room_event(ev: dict[str, Any], locale: Locale) -> dict[str, Any]:
    out = dict(ev)
    pairs = (
        ("impact_note_zh", "impact_note_en"),
        ("watch_zh", "watch_en"),
        ("deep_dive_zh", "deep_dive_en"),
        ("trade_implications_zh", "trade_implications_en"),
        ("scenario_zh", "scenario_en"),
        ("risk_watch_zh", "risk_watch_en"),
    )
    for zh_key, en_key in pairs:
        out[zh_key] = pick_text(
            zh=str(ev.get(zh_key) or ""),
            en=str(ev.get(en_key) or ""),
            locale=locale,
        )
    return out


def _call_war_room_llm(
    events: list[dict[str, Any]],
    treasury: dict[str, Any],
    hours: int,
    *,
    authors: Optional[list[str]] = None,
    locale: Locale = "zh",
) -> dict[str, Any] | None:
    cfg = get_settings()
    if not has_llm_provider(cfg) or not events:
        return None
    cache_key = _llm_cache_key(events, treasury, hours, authors=authors, locale=locale)
    cached = _get_cached_war_room_llm(events, treasury, hours, authors=authors, locale=locale)
    if cached:
        return cached

    prompt = {
        "window_hours": hours,
        "discord_events": events[:20],
        "treasury_curve": treasury,
        "locale": locale,
    }
    lang_line = (
        "Write all narrative fields in English."
        if locale == "en"
        else "所有解读字段使用中文。"
    )
    system = (
        f"{lang_line}\n"
        "你是华语美股盘前作战室分析师，服务对象以 SPY/QQQ/美股指数期权交易者为主。"
        "根据最近 Discord 消息和国债曲线，筛出真正影响今日交易的事件。"
        "不要把 GEX、Put/Call、call wall 这类市场结构指标当成事件。"
        "只输出 JSON：{\"events\":[{\"id\":\"\",\"title\":\"\",\"body\":\"\","
        "\"impact\":\"利好|利空|中性|风险\","
        "\"impact_scope\":\"equity_broad|oil_energy|rates_bonds|single_stock|macro_geo|mixed\","
        "\"impact_note_zh\":\"\","
        "\"impact_score\":0,\"related_assets\":[],\"watch_zh\":\"\","
        "\"deep_dive_zh\":\"\",\"trade_implications_zh\":\"\","
        "\"scenario_zh\":\"\",\"risk_watch_zh\":\"\"}],"
        "\"trade_plan\":[\"...\"],\"summary_zh\":\"...\"}。"
        "impact 必须表示对 SPY/QQQ/美股大盘风险偏好的方向，不是单一商品涨跌本身。"
        "例：美油暴跌但地缘缓和、通胀预期降温，对大盘可标利好或中性，勿仅因油价跌标利空。"
        "impact_scope 表示事件主要作用域：equity_broad=大盘指数；oil_energy=原油/能源链；"
        "rates_bonds=利率/国债；single_stock=个股；macro_geo=宏观/地缘；mixed=多资产分化。"
        "若油价/能源大跌但大盘偏正面，impact 按大盘写，oil 方向写在 impact_note_zh（如「能源链承压」）。"
        "related_assets 填最相关 ticker（如 SPY、QQQ、USO、XLE、TLT）。"
        "deep_dive_zh 解释为什么这是真事件而不是噪音；trade_implications_zh 说明对正股/期权行动的影响；"
        "scenario_zh 给出利好/利空两种盘中确认路径；risk_watch_zh 给出失效条件和风险。"
        "events 最多 5 条，trade_plan 最多 4 条；标题短，解读字段每条不超过 120 字。"
        "不得给确定收益承诺，不得编造价格。"
    )
    payload = {
        "temperature": 0.2,
        "max_tokens": 1800,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)[:14000]},
        ],
    }
    try:
        data, _provider = post_chat_completions_with_fallback(
            payload,
            cfg=cfg,
            source="mvp_war_room",
            timeout=90.0,
        )
        content = data["choices"][0]["message"].get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("empty LLM content")
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            start = content.find("{")
            end = content.rfind("}")
            if start < 0 or end <= start:
                raise
            parsed = json.loads(content[start : end + 1])
        if isinstance(parsed, dict):
            cache_set(cache_key, parsed, ttl=TTL_AI)
            return parsed
    except Exception as exc:
        logger.warning("MVP war-room LLM failed: %s", exc)
    return None


@router.get("/market-insights")
async def mvp_market_insights(
    background_tasks: BackgroundTasks,
    locale: str = Query(default="zh", pattern="^(zh|en)$"),
    entitlement: MvpEntitlement = Depends(resolve_mvp_entitlement),
) -> dict[str, Any]:
    """DeepAgents 深度推理：市场状态、VIX 曲线、VIX/P/C、国债曲线解读。"""
    loc = parse_locale(locale)
    try:
        context = build_mvp_market_context()
        cached = get_cached_mvp_market_insights(context, locale=loc)
        if cached is not None:
            payload = cached
        else:
            cache_key = market_insights_cache_key(context, loc)
            if has_llm_provider() and _should_schedule_llm(cache_key):
                background_tasks.add_task(_warm_market_insights_blocking, context, loc)
            payload = _rule_based_insights(context, loc)
    except Exception as exc:
        logger.exception("MVP market-insights failed: %s", exc)
        try:
            payload = _rule_based_insights(build_mvp_market_context(), loc)
        except Exception:
            payload = _rule_based_insights({}, loc)
    body = redact_market_insights(payload.model_dump(), entitlement.tier)
    return mvp_envelope(entitlement.tier, body)


@router.get("/macro-calendar-insights")
def mvp_macro_calendar_insights(
    background_tasks: BackgroundTasks,
    from_date: str = Query(""),
    to_date: str = Query(""),
    country: str = Query("US"),
    entitlement: MvpEntitlement = Depends(resolve_mvp_entitlement),
) -> dict[str, Any]:
    """AI/rule interpretation for economic calendar events in Chinese."""
    today = datetime.now(timezone.utc).date().isoformat()
    from_date = from_date or today
    to_date = to_date or from_date
    country = country or "US"
    try:
        calendar_payload = get_macro_calendar(from_date=from_date, to_date=to_date, country=country, impact="")
        raw_events = calendar_payload.get("events") if isinstance(calendar_payload, dict) else []
        events = _dedupe_macro_events([e for e in raw_events if isinstance(e, dict)])
    except Exception as exc:
        logger.warning("MVP macro-calendar-insights source failed: %s", exc)
        events = []

    fallback = _fallback_macro_calendar_insights(events)
    ai = _get_cached_macro_calendar_llm(from_date, to_date, country)
    macro_cache_key = _macro_insights_cache_key(from_date, to_date, country)
    if ai is None and events and _should_schedule_llm(macro_cache_key):
        background_tasks.add_task(_call_macro_calendar_llm, events, fallback, from_date, to_date, country)
    result = ai or fallback
    merged = {
        "from": from_date,
        "to": to_date,
        "country": country,
        **result,
    }
    body = redact_macro_calendar(merged, entitlement.tier)
    return mvp_envelope(entitlement.tier, body)


@router.get("/playbook-hints")
def mvp_playbook_hints(
    topic: str = Query(default="screener"),
    entitlement: MvpEntitlement = Depends(resolve_mvp_entitlement),
) -> dict[str, object]:
    """Short playbook bullets for MVP UI (no full HTML)."""
    from app.services.options_playbook import get_playbook_hints

    hint = get_playbook_hints(topic)
    body = redact_playbook_hints({"topic": hint.topic, "bullets": list(hint.bullets)}, entitlement.tier)
    return mvp_envelope(entitlement.tier, body)


@router.post("/stock-options-insights")
async def mvp_stock_options_insights(
    body: StockOptionsInsightRequest,
    locale: str = Query(default="zh", pattern="^(zh|en)$"),
    entitlement: MvpEntitlement = Depends(resolve_mvp_entitlement),
) -> dict[str, Any]:
    _ = parse_locale(locale)
    """阿吉深度洞察：期权合约筛选器 + Expected Move + 与异动/大盘对照。"""
    if entitlement.tier == "guest":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "login_required", "message": "请先注册并登录后查看标的深度分析。"},
        )
    try:
        cached = get_cached_stock_options_insights(body)
        if cached:
            payload = cached
        else:
            if entitlement.tier == "pro" and has_llm_provider():
                warm_key = f"mvp:stock-options-insights:warm:{body.symbol.upper()}:{body.direction}"
                if _should_schedule_llm(warm_key, cooldown_seconds=60):
                    loop = asyncio.get_running_loop()
                    task = loop.run_in_executor(None, _warm_stock_options_insights_blocking, body)
                    task.add_done_callback(_log_future_exception)
            payload = generate_fast_stock_options_insights(body)
    except Exception as exc:
        logger.exception("MVP stock-options-insights failed: %s", exc)
        from app.services.mvp_stock_options_agent import _rule_based_insights

        payload = _rule_based_insights(body)
    redacted = redact_stock_insights(payload.model_dump(), entitlement.tier)
    return mvp_envelope(entitlement.tier, redacted)


@router.get("/war-room")
def mvp_war_room(
    background_tasks: BackgroundTasks,
    hours: int = Query(default=6, ge=1, le=24),
    menu_slot: str = Query(default="aji_insights"),
    locale: str = Query(default="zh", pattern="^(zh|en)$"),
    session: Session = Depends(db_session_dep),
    entitlement: MvpEntitlement = Depends(resolve_mvp_entitlement),
) -> dict[str, Any]:
    loc = parse_locale(locale)
    author_filter = resolve_author_filter(session, menu_slot)
    try:
        rows = list_discord_feed_rows(
            session,
            ticker=None,
            hours=hours,
            limit=100,
            authors=author_filter,
        )
    except Exception as exc:
        logger.warning("MVP war-room discord query failed: %s", exc)
        rows = []
    discord_events = [_event_from_discord(r) for r in rows]
    try:
        latest_treasury = session.execute(
            select(TreasuryRateRow).order_by(TreasuryRateRow.rate_date.desc()).limit(1)
        ).scalars().first()
    except Exception as exc:
        logger.warning("MVP war-room treasury query failed: %s", exc)
        latest_treasury = None
    treasury = _treasury_read(latest_treasury)
    ai = _get_cached_war_room_llm(
        discord_events, treasury, hours, authors=author_filter, locale=loc
    )
    war_room_cache_key = _llm_cache_key(
        discord_events, treasury, hours, authors=author_filter, locale=loc
    )
    if ai is None and discord_events and _should_schedule_llm(war_room_cache_key):
        background_tasks.add_task(
            _call_war_room_llm,
            discord_events,
            treasury,
            hours,
            authors=author_filter,
            locale=loc,
        )

    events = [_localize_war_room_event(_normalize_war_room_event(e), loc) for e in discord_events[:8]]
    trade_plan = _fallback_plan(events, treasury)
    summary = ""
    if isinstance(ai, dict):
        ai_events = ai.get("events")
        ai_plan = ai.get("trade_plan")
        if isinstance(ai_events, list) and ai_events:
            events = [
                _localize_war_room_event(_normalize_war_room_event(e), loc)
                for e in ai_events
                if isinstance(e, dict)
            ][:8]
        if isinstance(ai_plan, list) and ai_plan:
            trade_plan = [str(x) for x in ai_plan if str(x).strip()][:6]
        summary = str(ai.get("summary_zh") or "").strip()

    raw = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "discord+treasury+llm" if ai else "discord+treasury+rules",
        "window_hours": hours,
        "events": events,
        "trade_plan": trade_plan,
        "treasury_read": treasury,
        "summary_zh": summary,
        "data_quality": {
            "discord_count": len(discord_events),
            "ai_enabled": bool(ai),
        },
    }
    body = redact_war_room(raw, entitlement.tier)
    return mvp_envelope(entitlement.tier, body)
