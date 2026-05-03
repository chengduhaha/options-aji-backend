"""LangGraph user Q&A workflow (Phase 2-lite: Discord 存档 + yfinance bundle)."""

from __future__ import annotations

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

logger = logging.getLogger(__name__)


class UserAgentState(TypedDict, total=False):
    """Graph state propagated between nodes."""

    question: str
    ticker_hint: str
    resolved_ticker: str
    discord_context: str
    market_bundle: str
    answer: str


def gather_discord_snapshot(state: UserAgentState) -> dict[str, str]:
    cfg = get_settings()

    qs = state.get("question", "").strip()

    spotlight, filt = infer_message_filter_symbol(
        question=qs,
        ticker_hint=state.get("ticker_hint") or "",
    )

    blob, qty = format_discord_digest(filter_sym=filt, cfg=cfg)

    suffix = ""
    if qty > 0:
        suffix = f"\n（共载入 {qty} 条存档）"

    return {
        "resolved_ticker": spotlight,
        "discord_context": blob + suffix,
    }


def fetch_market_bundle(state: UserAgentState) -> dict[str, str]:
    guard_q = state.get("question", "").strip()
    if not guard_q:
        return {"market_bundle": "{}"}

    ticker = state.get("resolved_ticker") or "SPY"
    toolkit = build_default_toolkit()
    bundle = toolkit.snapshot_bundle(ticker)
    return {"market_bundle": bundle}


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

    system = (
        "你是美股期权与市场结构分析师 OptionsAji。必须用中文作答，简明、可执行。\n"
        "综合参考「Discord存档摘要」（若标明无数据则说明缺口）与市场 JSON 数据结构；不自造成交价。\n"
        "风险提示：教育是目的，不构成投资建议。"
    )

    human = HumanMessage(
        content=(
            f"用户提问：{state.get('question', '')}\n"
            f"主要标的代码：{state.get('resolved_ticker', '')}\n"
            f"Discord存档：\n{state.get('discord_context', '').strip()}\n"
            f"市场行情 JSON：\n{state.get('market_bundle', '{}')}"
        ),
    )

    try:
        out = llm.invoke([SystemMessage(content=system), human])
        text = getattr(out, "content", None)
        payload = "" if text is None else str(text)
        return {"answer": payload}
    except Exception as exc:
        logger.exception("LLM synthesize failure: %s", exc)
        return {"answer": f"调用语言模型失败，请稍后重试。详情：{type(exc).__name__}"}


def build_initial_agent_state(*, question: str, ticker: Optional[str]) -> UserAgentState:
    """Warm state before running pipeline phases (SSE + sync invoke reuse)."""

    guard = question.strip()
    ticker_hint = (ticker or "").strip()
    return {
        "question": guard,
        "ticker_hint": ticker_hint,
    }


def execute_user_agent_pipeline(initial: UserAgentState) -> UserAgentState:
    """Run Discord → market → synthesize sequentially (deterministic replay)."""

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

    state: UserAgentState = {
        **initial,
        "question": guard,
    }
    state.update(gather_discord_snapshot(state))
    state.update(fetch_market_bundle(state))
    state.update(synthesize_llm_answer(state))
    return state


def run_user_agent_once(*, question: str, ticker: Optional[str]) -> UserAgentState:
    """Execute workflow synchronously (FastAPI SSE worker thread / tests)."""

    initial = build_initial_agent_state(question=question, ticker=ticker)
    return execute_user_agent_pipeline(initial)
