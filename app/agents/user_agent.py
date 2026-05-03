"""LangGraph user Q&A workflow (Phase 1: parse via regex + bundled market snapshot)."""

from __future__ import annotations

import logging

from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from app.config import get_settings
from app.ingest.tickers import extract_tickers
from app.tools.openbb_tools import build_default_toolkit

logger = logging.getLogger(__name__)


class UserAgentState(TypedDict, total=False):
    """Graph state propagated between nodes."""

    question: str
    ticker_hint: str
    resolved_ticker: str
    market_bundle: str
    answer: str


def _fetch_market_data(state: UserAgentState) -> dict[str, str]:
    guard_q = state.get("question", "").strip()
    if not guard_q:
        return {
            "resolved_ticker": "SPY",
            "market_bundle": "{}",
        }

    ticker = ""
    hint = state.get("ticker_hint") or ""
    if hint.strip():
        ticker = hint.strip().upper()

    if not ticker:
        found = extract_tickers(guard_q)
        ticker = found[0] if found else "SPY"

    toolkit = build_default_toolkit()
    bundle = toolkit.snapshot_bundle(ticker)
    return {"resolved_ticker": ticker, "market_bundle": bundle}


def _synthesize(state: UserAgentState) -> dict[str, str]:
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
        "只依据用户问题与给定 JSON 结构化数据推演；若无数据则说明缺口，不自造行情。\n"
        "风险提示：教育是目的，不构成投资建议。"
    )

    human = HumanMessage(
        content=(
            f"用户提问：{state.get('question', '')}\n"
            f"主要标的代码：{state.get('resolved_ticker', '')}\n"
            f"市场数据结构：\n{state.get('market_bundle', '{}')}"
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


_built_graph = None


def build_user_agent_graph():
    """Return compiled graph singleton."""
    global _built_graph  # noqa: PLW0603
    if _built_graph is not None:
        return _built_graph

    sg = StateGraph(UserAgentState)
    sg.add_node("fetch_market_data", _fetch_market_data)
    sg.add_node("synthesize", _synthesize)
    sg.add_edge(START, "fetch_market_data")
    sg.add_edge("fetch_market_data", "synthesize")
    sg.add_edge("synthesize", END)

    compiled = sg.compile()
    _built_graph = compiled
    return compiled


def run_user_agent_once(*, question: str, ticker: Optional[str]) -> UserAgentState:
    """Execute full graph synchronously (FastAPI SSE worker thread)."""

    guard = question.strip()
    if not guard:
        return {
            "question": "",
            "answer": "问题不能为空。",
            "resolved_ticker": "SPY",
            "market_bundle": "{}",
        }

    graph = build_user_agent_graph()
    merged: UserAgentState = graph.invoke(  # type: ignore[arg-type]
        {"question": guard, "ticker_hint": (ticker or "").strip()},
    )
    return merged
