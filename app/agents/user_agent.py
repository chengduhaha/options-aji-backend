"""LangGraph user Q&A workflow v2 — real data context + mode routing."""

from __future__ import annotations

import json
import logging
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from typing_extensions import TypedDict

from app.agents.user_agent_helpers import (
    format_discord_digest,
    infer_message_filter_symbol,
)
from app.config import get_settings
from app.tools.openbb_tools import build_default_toolkit
from app.clients.fmp_client import get_fmp_client

logger = logging.getLogger(__name__)


class UserAgentState(TypedDict, total=False):
    question: str
    ticker_hint: str
    mode: str  # "fast" | "analysis" | "strategy"
    resolved_ticker: str
    discord_context: str
    market_bundle: str
    answer: str


def _safe_json(obj: object) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        return "{}"


def _fetch_real_data_context(symbol: str) -> dict[str, object]:
    """Build rich market context from all available data sources."""
    ctx: dict[str, object] = {"symbol": symbol}
    tk = build_default_toolkit()
    cfg = get_settings()

    # 1. Quote + market bar
    try:
        bar = tk.frontend_market_bar(symbol)
        if bar:
            ctx["market_bar"] = bar
    except Exception:
        pass

    # 2. GEX profile
    try:
        gex = tk.get_gex(symbol)
        if gex and isinstance(gex, dict) and not gex.get("error"):
            ctx["gex"] = {
                "netGex_bn": gex.get("netGex"),
                "regime": gex.get("regime"),
                "gammaFlip": gex.get("gammaFlip"),
                "maxPain": gex.get("maxPain"),
                "callWall": gex.get("callWall"),
                "putWall": gex.get("putWall"),
                "strikes_count": len(gex.get("strikes", [])),
                "underlyingPrice": gex.get("underlyingPrice"),
            }
    except Exception:
        pass

    # 3. Option chain (front expiry)
    try:
        chain = tk.get_option_chain_full(symbol)
        if chain and isinstance(chain, dict):
            calls = chain.get("calls", [])
            puts = chain.get("puts", [])
            if isinstance(calls, list) and isinstance(puts, list):
                atm = None
                spot = bar.get("spot") or bar.get("price") or 0
                if spot:
                    all_contracts = []
                    for c in calls:
                        if isinstance(c, dict):
                            c["type"] = "call"
                            all_contracts.append(c)
                    for p in puts:
                        if isinstance(p, dict):
                            p["type"] = "put"
                            all_contracts.append(p)
                    if all_contracts:
                        all_contracts.sort(key=lambda x: abs(float(x.get("strike", 0) or 0) - float(spot)))
                        atm_contracts = all_contracts[:6]
                        ctx["atm_options"] = [
                            {
                                "type": c.get("type"),
                                "strike": c.get("strike"),
                                "bid": c.get("bid"),
                                "ask": c.get("ask"),
                                "iv": c.get("impliedVolatility"),
                                "delta": c.get("delta"),
                                "gamma": c.get("gamma"),
                                "theta": c.get("theta"),
                                "vega": c.get("vega"),
                                "oi": c.get("openInterest"),
                                "volume": c.get("day_volume"),
                            }
                            for c in atm_contracts
                        ]
                expiry = chain.get("expiration")
                if expiry:
                    ctx["front_expiry"] = str(expiry)
    except Exception:
        pass

    # 4. Analyst ratings + price target
    if cfg.fmp_api_key:
        try:
            fmp = get_fmp_client()
            pt = fmp.get_price_target_summary(symbol)
            if pt:
                ctx["price_target"] = {
                    "lastMonthAvg": pt.get("lastMonthAvgPriceTarget"),
                    "lastMonthCount": pt.get("lastMonthCount"),
                    "allTimeAvg": pt.get("allTimeAvgPriceTarget"),
                }
            ratings = fmp.get_analyst_ratings(symbol)
            if ratings and isinstance(ratings, list):
                recent = ratings[:5]
                ctx["recent_ratings"] = [
                    {"firm": r.get("gradingCompany"), "action": r.get("action"),
                     "to": r.get("newGrade"), "from": r.get("previousGrade"),
                     "target": r.get("priceTarget"), "date": r.get("date")}
                    for r in recent if isinstance(r, dict)
                ]
        except Exception:
            pass

    # 5. Earnings context
    try:
        ctx["earnings"] = tk.snapshot_bundle(symbol).get("earnings")
    except Exception:
        pass

    return ctx


def gather_discord_snapshot(state: UserAgentState) -> dict[str, str]:
    cfg = get_settings()
    qs = state.get("question", "").strip()
    spotlight, filt = infer_message_filter_symbol(
        question=qs,
        ticker_hint=state.get("ticker_hint") or "",
    )
    blob, qty = format_discord_digest(filter_sym=filt, cfg=cfg)
    suffix = f"\n（共载入 {qty} 条存档）" if qty > 0 else ""
    return {"resolved_ticker": spotlight, "discord_context": blob + suffix}


def fetch_market_bundle(state: UserAgentState) -> dict[str, str]:
    guard_q = state.get("question", "").strip()
    if not guard_q:
        return {"market_bundle": "{}"}
    ticker = state.get("resolved_ticker") or "SPY"
    # Use rich real-data context instead of yfinance-only snapshot
    bundle = _fetch_real_data_context(ticker)
    return {"market_bundle": _safe_json(bundle)}


def synthesize_llm_answer(state: UserAgentState) -> dict[str, str]:
    cfg = get_settings()
    api_key = cfg.openrouter_api_key.strip()
    if not api_key:
        return {"answer": "服务端未配置 OPENROUTER_API_KEY，无法调用语言模型。"}

    llm = ChatOpenAI(
        api_key=api_key,
        base_url=cfg.openrouter_base_url,
        model=cfg.model_synthesis,
        temperature=0.25,
        timeout=120,
        max_retries=2,
    )

    mode = state.get("mode", "fast")
    ticker = state.get("resolved_ticker") or "SPY"

    base_prompt = (
        "你是美股期权与市场结构分析师 OptionsAji。必须用中文作答。\n"
        "参考提供的市场数据和 Discord 存档；不自造成交价。\n"
        "风险提示：教育是目的，不构成投资建议。\n"
    )

    mode_instructions = {
        "fast": (
            "模式：快速问答。\n"
            "用 3-5 句话直接回答用户问题，聚焦关键数据点。\n"
            "适合快速了解行情、GEX 环境、IV 水平等。\n"
        ),
        "analysis": (
            "模式：深度分析。\n"
            "对用户问题进行深入的结构化分析，包含：\n"
            "1. 市场环境（价格、波动率、GEX 画像）\n"
            "2. 期权数据解读（IV 期限结构、Skew、持仓分布）\n"
            "3. 关键风险点\n"
            "4. 多时间维度视角\n"
            "使用数据支撑结论，标注关键数值。\n"
        ),
        "strategy": (
            "模式：策略评估。\n"
            "根据用户的方向判断（看涨/看跌/中性）和期限偏好，推荐具体策略：\n"
            "1. 策略名称与构建方式（用哪些合约）\n"
            "2. 最大收益 / 最大亏损 / 盈亏平衡点\n"
            "3. Greeks 暴露分析\n"
            "4. 隐含概率参考\n"
            "给出 1-2 个备选策略比较优劣。\n"
        ),
    }

    sys_prompt = base_prompt + mode_instructions.get(mode, mode_instructions["fast"])

    human = HumanMessage(
        content=(
            f"用户提问：{state.get('question', '')}\n"
            f"主要标的代码：{ticker}\n"
            f"模式：{mode}\n"
            f"Discord存档：\n{state.get('discord_context', '').strip()}\n"
            f"市场数据 JSON：\n{state.get('market_bundle', '{}')}"
        ),
    )

    try:
        out = llm.invoke([SystemMessage(content=sys_prompt), human])
        text = getattr(out, "content", None)
        return {"answer": "" if text is None else str(text)}
    except Exception as exc:
        logger.exception("LLM synthesize failure: %s", exc)
        return {"answer": f"调用语言模型失败，请稍后重试。详情：{type(exc).__name__}"}


def build_initial_agent_state(
    *, question: str, ticker: Optional[str], mode: str = "fast"
) -> UserAgentState:
    guard = question.strip()
    ticker_hint = (ticker or "").strip()
    return {"question": guard, "ticker_hint": ticker_hint, "mode": mode}


def execute_user_agent_pipeline(initial: UserAgentState) -> UserAgentState:
    guard = initial.get("question", "").strip()
    if not guard:
        return {
            **initial,
            "question": "",
            "answer": "问题不能为空。",
            "resolved_ticker": initial.get("ticker_hint", "") or "SPY",
            "discord_context": "",
            "market_bundle": "{}",
        }

    state: UserAgentState = {**initial, "question": guard}
    state.update(gather_discord_snapshot(state))
    state.update(fetch_market_bundle(state))
    state.update(synthesize_llm_answer(state))
    return state


def run_user_agent_once(
    *, question: str, ticker: Optional[str], mode: str = "fast"
) -> UserAgentState:
    initial = build_initial_agent_state(question=question, ticker=ticker, mode=mode)
    return execute_user_agent_pipeline(initial)