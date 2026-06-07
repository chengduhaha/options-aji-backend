"""MVP 市场总览 — DeepAgents 深度推理 + 规则兜底。"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.analytics.market_regime import (
    RegimeCode,
    classify_regime_from_metrics,
    normalize_regime_code,
    regime_label,
)
from app.services.cache_service import TTL_AI, cache_get, cache_set
from app.services.locale import Locale, parse_locale
from app.services.llm_router import build_chat_openai, configured_providers, has_llm_provider

try:
    from deepagents import create_deep_agent
except ModuleNotFoundError:
    create_deep_agent = None

logger = logging.getLogger(__name__)

EngineKind = Literal["deepagents", "fallback", "rules"]


class RegimeInsight(BaseModel):
    code: RegimeCode = Field(description="risk_off | elevated_vol | risk_on | range_bound | transitional")
    label: str = Field(description="与 code 对应的中文展示名")
    summary: str
    reasoning: str = ""
    basis: list[str] = Field(default_factory=list)


class VixChartInsight(BaseModel):
    caption: str = Field(description="黄色 VIX 迷你曲线走势解读")


class VixPcrInsight(BaseModel):
    interpretation: str


class TreasuryInsight(BaseModel):
    label: str
    summary: str
    spreads: dict[str, float | None] = Field(default_factory=dict)


class MvpMarketInsightsPayload(BaseModel):
    regime: RegimeInsight
    vix_chart: VixChartInsight
    vix: VixPcrInsight
    pcr: VixPcrInsight
    treasury: TreasuryInsight
    engine: EngineKind
    generated_at_utc: str
    cached: bool = False


def _five_minute_bucket_utc() -> str:
    now = datetime.now(timezone.utc)
    bucket_min = (now.minute // 5) * 5
    return now.replace(minute=bucket_min, second=0, microsecond=0).isoformat()


def _cache_key(context: dict[str, Any], locale: Locale) -> str:
    raw = json.dumps(
        {"bucket": _five_minute_bucket_utc(), "ctx": _context_fingerprint(context), "locale": locale},
        ensure_ascii=False,
        sort_keys=True,
    )
    return f"mvp:market-insights:v2:{hash(raw)}"


def _context_fingerprint(ctx: dict[str, Any]) -> dict[str, Any]:
    ov = ctx.get("overview") if isinstance(ctx.get("overview"), dict) else {}
    vol = ov.get("volatility") if isinstance(ov.get("volatility"), dict) else {}
    liq = ov.get("liquidity") if isinstance(ov.get("liquidity"), dict) else {}
    pulse = ov.get("pulse") if isinstance(ov.get("pulse"), list) else []
    sigs = ctx.get("signals") if isinstance(ctx.get("signals"), dict) else {}
    treasury = ctx.get("treasury") if isinstance(ctx.get("treasury"), dict) else {}
    return {
        "pulse": [
            {"s": p.get("symbol"), "p": p.get("price"), "c": p.get("changePct")}
            for p in pulse[:6]
            if isinstance(p, dict)
        ],
        "vix": vol.get("vix"),
        "vix_chg": vol.get("vixChangePct"),
        "vix_series": vol.get("vixSeries"),
        "band": vol.get("band"),
        "pcr": liq.get("putCallRatioVolumeApprox"),
        "pcr_cboe": liq.get("putCallRatioEquityCboe"),
        "signal_count": len(sigs.get("signals", [])) if isinstance(sigs.get("signals"), list) else 0,
        "treasury": treasury.get("rates", [{}])[0] if treasury.get("rates") else {},
    }


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _rule_based_insights(context: dict[str, Any], locale: Locale = "zh") -> MvpMarketInsightsPayload:
    ov = context.get("overview") if isinstance(context.get("overview"), dict) else {}
    vol = ov.get("volatility") if isinstance(ov.get("volatility"), dict) else {}
    liq = ov.get("liquidity") if isinstance(ov.get("liquidity"), dict) else {}
    pulse = ov.get("pulse") if isinstance(ov.get("pulse"), list) else []
    signals_raw = context.get("signals") if isinstance(context.get("signals"), dict) else {}
    signals = signals_raw.get("signals") if isinstance(signals_raw.get("signals"), list) else []

    spy_chg = None
    qqq_chg = None
    for row in pulse:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol", ""))
        cp = _num(row.get("changePct"))
        if sym == "SPY":
            spy_chg = cp
        if sym == "QQQ":
            qqq_chg = cp

    vix = _num(vol.get("vix"))
    vix_chg = _num(vol.get("vixChangePct"))
    band = str(vol.get("band") or "—")
    vix_series = [float(x) for x in vol.get("vixSeries", []) if isinstance(x, (int, float))]

    avg_index = 0.0
    if spy_chg is not None and qqq_chg is not None:
        avg_index = (spy_chg + qqq_chg) / 2
    elif spy_chg is not None:
        avg_index = spy_chg
    elif qqq_chg is not None:
        avg_index = qqq_chg

    signal_score = 0
    for sig in signals:
        if not isinstance(sig, dict):
            continue
        d = sig.get("direction")
        st = int(sig.get("strength") or 0)
        if d == "bull":
            signal_score += st
        elif d == "bear":
            signal_score -= st

    if locale == "en":
        basis = [
            f"SPY {spy_chg if spy_chg is not None else '—'}%, QQQ {qqq_chg if qqq_chg is not None else '—'}%",
            f"VIX {vix if vix is not None else '—'} ({band}), daily {vix_chg if vix_chg is not None else '—'}%",
            f"Signal score {signal_score}",
        ]
    else:
        basis = [
            f"SPY 涨跌 {spy_chg if spy_chg is not None else '—'}%，QQQ {qqq_chg if qqq_chg is not None else '—'}%",
            f"VIX {vix if vix is not None else '—'}（{band}），日变化 {vix_chg if vix_chg is not None else '—'}%",
            f"信号综合得分 {signal_score}",
        ]

    code, label, summary, reasoning = classify_regime_from_metrics(
        avg_index=avg_index,
        vix=vix,
        vix_chg=vix_chg,
        vix_band=band,
        signal_score=signal_score,
        locale=locale,
    )

    if len(vix_series) >= 2:
        first, last = vix_series[0], vix_series[-1]
        delta = last - first
        if locale == "en":
            if delta > 1.5:
                chart_cap = (
                    f"Over {len(vix_series)} sessions VIX rose from {first:.1f} to {last:.1f}; "
                    "fear is building and option premiums/hedge demand are rising."
                )
            elif delta < -1.5:
                chart_cap = (
                    f"Over {len(vix_series)} sessions VIX fell from {first:.1f} to {last:.1f}; "
                    "vol premium is easing and risk appetite is repairing."
                )
            else:
                chart_cap = (
                    f"VIX ranged {min(vix_series):.1f}–{max(vix_series):.1f} over {len(vix_series)} "
                    "sessions without a one-sided vol shock."
                )
        elif delta > 1.5:
            chart_cap = f"近 {len(vix_series)} 日 VIX 由 {first:.1f} 升至 {last:.1f}，恐慌情绪升温，期权溢价与对冲需求抬升。"
        elif delta < -1.5:
            chart_cap = f"近 {len(vix_series)} 日 VIX 由 {first:.1f} 回落至 {last:.1f}，波动溢价回落，风险偏好修复中。"
        else:
            chart_cap = f"近 {len(vix_series)} 日 VIX 在 {min(vix_series):.1f}–{max(vix_series):.1f} 区间震荡，波动环境未出现单边恶化。"
    else:
        chart_cap = (
            "Insufficient VIX history; rely on spot level and band label."
            if locale == "en"
            else "VIX 历史序列不足，暂以当日水平与区间标签为主判断。"
        )

    if vix is not None:
        if locale == "en":
            if vix > 30:
                vix_txt = f"VIX {vix:.1f} is in panic territory — avoid naked short vol; prioritize hedges."
            elif vix > 20:
                vix_txt = f"VIX {vix:.1f} is elevated; premiums are rich and sellers need wider cushions."
            elif vix < 13:
                vix_txt = f"VIX {vix:.1f} is very low; watch for mean reversion in vol."
            else:
                vix_txt = f"VIX {vix:.1f} is in a normal band; read it with curve shape."
        elif vix > 30:
            vix_txt = f"VIX {vix:.1f} 处于恐慌区，避免裸卖期权，优先对冲与降杠杆。"
        elif vix > 20:
            vix_txt = f"VIX {vix:.1f} 偏高，期权溢价明显，卖方需更宽安全边际。"
        elif vix < 13:
            vix_txt = f"VIX {vix:.1f} 极低，警惕波动率均值回归。"
        else:
            vix_txt = f"VIX {vix:.1f} 处于常规区间，结合曲线形态判断方向。"
    else:
        vix_txt = "VIX data unavailable." if locale == "en" else "VIX 数据缺失。"

    pcr = _num(liq.get("putCallRatioVolumeApprox")) or _num(liq.get("putCallRatioEquityCboe"))
    if pcr is None:
        pcr_txt = "P/C data unavailable." if locale == "en" else "P/C 数据缺失。"
    elif locale == "en":
        if pcr > 1.2:
            pcr_txt = f"P/C {pcr:.2f}: heavy put activity — cautious tone; watch for sentiment reversals."
        elif pcr > 1:
            pcr_txt = f"P/C {pcr:.2f}: puts relatively active; hedging demand is rising."
        elif pcr < 0.5:
            pcr_txt = f"P/C {pcr:.2f}: call surge — bullish chase; pullback risk rises."
        elif pcr < 0.7:
            pcr_txt = f"P/C {pcr:.2f}: calls relatively active; risk appetite optimistic."
        else:
            pcr_txt = f"P/C {pcr:.2f}: call/put volume fairly balanced."
    elif pcr > 1.2:
        pcr_txt = f"P/C {pcr:.2f}：Put 异常活跃，情绪偏谨慎，需警惕过度悲观后的反向波动。"
    elif pcr > 1:
        pcr_txt = f"P/C {pcr:.2f}：Put 相对活跃，对冲与防守需求上升。"
    elif pcr < 0.5:
        pcr_txt = f"P/C {pcr:.2f}：Call 异常活跃，追涨情绪浓，注意回调风险。"
    elif pcr < 0.7:
        pcr_txt = f"P/C {pcr:.2f}：Call 偏活跃，风险偏好偏乐观。"
    else:
        pcr_txt = f"P/C {pcr:.2f}：多空成交量相对均衡。"

    treasury = _treasury_from_context(context, locale=locale)
    return MvpMarketInsightsPayload(
        regime=RegimeInsight(code=code, label=label, summary=summary, reasoning=reasoning, basis=basis),
        vix_chart=VixChartInsight(caption=chart_cap),
        vix=VixPcrInsight(interpretation=vix_txt),
        pcr=VixPcrInsight(interpretation=pcr_txt),
        treasury=treasury,
        engine="rules",
        generated_at_utc=datetime.now(timezone.utc).isoformat(),
    )


def _treasury_from_context(context: dict[str, Any], *, locale: Locale = "zh") -> TreasuryInsight:
    tr = context.get("treasury") if isinstance(context.get("treasury"), dict) else {}
    rates = tr.get("rates") if isinstance(tr.get("rates"), list) else []
    latest = rates[0] if rates and isinstance(rates[0], dict) else {}
    month1 = _num(latest.get("month1") or latest.get("1M"))
    year2 = _num(latest.get("year2") or latest.get("2Y"))
    year10 = _num(latest.get("year10") or latest.get("10Y"))
    year30 = _num(latest.get("year30") or latest.get("30Y"))
    spread_10y_2y = year10 - year2 if year10 is not None and year2 is not None else None
    spread_30y_10y = year30 - year10 if year30 is not None and year10 is not None else None
    spread_10y_1m = year10 - month1 if year10 is not None and month1 is not None else None
    spreads = {
        "10y_2y": spread_10y_2y,
        "30y_10y": spread_30y_10y,
        "10y_1m": spread_10y_1m,
    }
    if spread_10y_2y is None:
        return TreasuryInsight(
            label="Awaiting rates" if locale == "en" else "等待利率数据",
            summary=(
                "Key Treasury tenors missing; use the yield chart only as a level reference."
                if locale == "en"
                else "国债曲线缺少关键期限，暂时只把柱状图作为利率水平参考。"
            ),
            spreads=spreads,
        )
    if spread_10y_2y < -0.25:
        return TreasuryInsight(
            label="Deep inversion" if locale == "en" else "曲线深度倒挂",
            summary=(
                "2Y above 10Y — market still prices cuts/slowdown; growth rebounds need lower "
                "rates and better risk appetite."
                if locale == "en"
                else "2Y 高于 10Y，市场仍在交易降息和增长放缓预期；成长股反弹更依赖利率回落和风险偏好修复。"
            ),
            spreads=spreads,
        )
    if spread_10y_2y < 0:
        return TreasuryInsight(
            label="Mild inversion" if locale == "en" else "曲线轻度倒挂",
            summary=(
                "Front end still above the belly; watch whether 10Y keeps rising — higher long "
                "rates pressure QQQ/NVDA duration."
                if locale == "en"
                else "短端仍高于长端，但倒挂不深；盘中重点看 10Y 是否继续上行，长端上行会压制 QQQ/NVDA 这类久期资产。"
            ),
            spreads=spreads,
        )
    if spread_30y_10y is not None and spread_30y_10y > 0.25:
        return TreasuryInsight(
            label="Steep long end" if locale == "en" else "长端偏陡",
            summary=(
                "30Y rich vs 10Y — term premium rising; if USD strengthens, chase growth more carefully."
                if locale == "en"
                else "30Y 相对 10Y 偏高，长端期限溢价抬升；若伴随美元走强，成长股追高需降仓位。"
            ),
            spreads=spreads,
        )
    return TreasuryInsight(
        label="Normal curve" if locale == "en" else "曲线相对正常",
        summary=(
            "2Y/10Y not deeply inverted; curve is a smaller headwind — focus on index/vol confirmation."
            if locale == "en"
            else "2Y/10Y 未明显倒挂，利率曲线对风险资产的压制相对有限，盘中更应关注指数和波动率确认。"
        ),
        spreads=spreads,
    )


def _parse_agent_json(content: str) -> dict[str, Any] | None:
    text = content.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        try:
            parsed = json.loads(fenced.group(1))
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _last_message_content(result: Any) -> str:
    messages = result.get("messages") if isinstance(result, dict) else getattr(result, "messages", None)
    if not isinstance(messages, list) or not messages:
        return ""
    last = messages[-1]
    if isinstance(last, dict):
        return str(last.get("content") or "")
    return str(getattr(last, "content", "") or "")


def _payload_from_parsed(parsed: dict[str, Any], engine: EngineKind) -> MvpMarketInsightsPayload | None:
    try:
        regime_raw = parsed.get("regime") if isinstance(parsed.get("regime"), dict) else {}
        vix_chart_raw = parsed.get("vix_chart") if isinstance(parsed.get("vix_chart"), dict) else {}
        vix_raw = parsed.get("vix") if isinstance(parsed.get("vix"), dict) else {}
        pcr_raw = parsed.get("pcr") if isinstance(parsed.get("pcr"), dict) else {}
        treasury_raw = parsed.get("treasury") if isinstance(parsed.get("treasury"), dict) else {}
        code = normalize_regime_code(regime_raw.get("code"), regime_raw.get("label"))
        if code is None:
            return None
        label = str(regime_raw.get("label") or "").strip() or regime_label(code)
        spreads_in = treasury_raw.get("spreads")
        spreads: dict[str, float | None] = {}
        if isinstance(spreads_in, dict):
            for k, v in spreads_in.items():
                spreads[str(k)] = _num(v)
        return MvpMarketInsightsPayload(
            regime=RegimeInsight(
                code=code,
                label=label,
                summary=str(regime_raw.get("summary") or "").strip()[:400],
                reasoning=str(regime_raw.get("reasoning") or "").strip()[:600],
                basis=[str(b) for b in regime_raw.get("basis", []) if str(b).strip()][:6],
            ),
            vix_chart=VixChartInsight(caption=str(vix_chart_raw.get("caption") or "").strip()[:500]),
            vix=VixPcrInsight(interpretation=str(vix_raw.get("interpretation") or "").strip()[:400]),
            pcr=VixPcrInsight(interpretation=str(pcr_raw.get("interpretation") or "").strip()[:400]),
            treasury=TreasuryInsight(
                label=str(treasury_raw.get("label") or "国债曲线").strip()[:80],
                summary=str(treasury_raw.get("summary") or "").strip()[:500],
                spreads=spreads,
            ),
            engine=engine,
            generated_at_utc=datetime.now(timezone.utc).isoformat(),
        )
    except Exception as exc:
        logger.warning("mvp market insights parse failed: %s", exc)
        return None


_MVP_AGENT: Any = None


def _build_mvp_market_agent() -> Any:
    global _MVP_AGENT
    should_cache_agent = len(configured_providers()) <= 1
    if should_cache_agent and _MVP_AGENT is not None:
        return _MVP_AGENT

    model = build_chat_openai(
        openrouter_model=os.getenv("MVP_MARKET_MODEL", "").strip()
        or os.getenv("COPILOT_MODEL", "").strip()
        or os.getenv("SUPERVISOR_MODEL", "").strip()
        or None,
        xiaomi_model=os.getenv("MVP_XIAOMI_MODEL", "").strip() or None,
        source="mvp_market_insights",
        temperature=0.25,
    )
    instructions = """你是 OptionsAji 盘前战略中心的首席市场分析师（华语美股期权交易者视角）。

你将收到 JSON 格式的市场快照（指数 pulse、VIX 序列与区间、P/C、信号 feed、国债收益率曲线）。
请进行**深度推理**（先综合宏观与微观再下结论），但**只输出一个 JSON 对象**，不要 Markdown，不要买卖指令。

输出 schema（字段名必须一致）：
{
  "regime": {
    "code": "risk_off | elevated_vol | risk_on | range_bound | transitional 五选一",
    "label": "与 code 对应中文：避险环境 | 高波动环境 | 风险偏好 | 中性震荡 | 过渡观察",
    "summary": "客观描述当前盘面环境（非买卖建议），≤90字",
    "reasoning": "2-3句推理链，说明为何是该环境分类",
    "basis": ["依据1", "依据2", "依据3"]
  },
  "vix_chart": {
    "caption": "解读黄色 VIX 迷你曲线近N日走势、拐点与对期权溢价/对冲的含义，≤120字"
  },
  "vix": { "interpretation": "当日 VIX 水平+日变化的交易含义，≤100字" },
  "pcr": { "interpretation": "Put/Call 比的情绪与反向信号提示，≤100字" },
  "treasury": {
    "label": "曲线标签，如 深度倒挂/轻度倒挂/长端偏陡/相对正常",
    "summary": "对 SPY/QQQ/成长股与期权策略的影响，≤120字",
    "spreads": { "10y_2y": null, "30y_10y": null, "10y_1m": null }
  }
}

环境分类说明（机构常用框架，勿用喊单用语）：
- risk_off / 避险环境：Risk-Off，避险偏好抬升
- elevated_vol / 高波动环境：波动率偏高、方向未一致
- risk_on / 风险偏好：Risk-On，风险资产相对受青睐（≠ 建议做多）
- range_bound / 中性震荡：指数波动有限、区间特征
- transitional / 过渡观察：指标分歧、暂无主导环境

约束：
- 不得编造未提供的数据；缺失则写明不确定
- 不得承诺收益、不得使用「进攻/抄底/必涨」等喊单措辞
- 用「环境/结构/偏好/波动」等客观表述
- 英文术语可保留：VIX, IV, GEX, P/C, Risk-On, Risk-Off
"""

    if create_deep_agent is None:
        from app.cross_market.copilot_supervisor import FallbackAgent

        agent = FallbackAgent(model=model, instructions=instructions)
        if should_cache_agent:
            _MVP_AGENT = agent
        return agent

    agent = create_deep_agent(model=model, tools=[], system_prompt=instructions)
    if should_cache_agent:
        _MVP_AGENT = agent
    return agent


async def generate_mvp_market_insights(
    context: dict[str, Any],
    *,
    locale: Locale = "zh",
) -> MvpMarketInsightsPayload:
    """DeepAgents 推理；失败则规则兜底。"""
    loc = parse_locale(locale)
    cache_key = _cache_key(context, loc)
    cached = cache_get(cache_key)
    if isinstance(cached, dict):
        try:
            payload = MvpMarketInsightsPayload.model_validate(cached)
            payload.cached = True
            return payload
        except Exception:
            pass

    if not has_llm_provider():
        logger.info("No LLM provider configured; MVP market insights use rules")
        return _rule_based_insights(context, loc)

    agent = _build_mvp_market_agent()
    user_payload = json.dumps(context, ensure_ascii=False)[:16000]
    lang_hint = (
        "Output all text fields in English."
        if loc == "en"
        else "所有文本字段使用中文。"
    )
    prompt = (
        f"{lang_hint}\n根据以下市场快照输出 JSON（严格遵循 system 中的 schema）：\n\n"
        f"{user_payload}"
    )
    engine: EngineKind = "deepagents" if create_deep_agent is not None else "fallback"
    try:
        result = await agent.ainvoke({"messages": [{"role": "user", "content": prompt}]})
        content = _last_message_content(result)
        parsed = _parse_agent_json(content)
        if parsed:
            built = _payload_from_parsed(parsed, engine)
            if built and built.regime.summary and built.vix_chart.caption:
                if not built.treasury.spreads:
                    rule_t = _treasury_from_context(context, locale=loc)
                    built.treasury.spreads = rule_t.spreads
                cache_set(cache_key, built.model_dump(), ttl=TTL_AI)
                return built
    except Exception as exc:
        logger.warning("MVP market insights agent failed: %s", exc)

    fallback = _rule_based_insights(context, loc)
    cache_set(cache_key, fallback.model_dump(), ttl=min(TTL_AI, 300))
    return fallback
