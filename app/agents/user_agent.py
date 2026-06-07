"""User agent Q&A — cache-first data + adaptive single prompt."""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage
from typing_extensions import TypedDict

from app.agents.user_agent_helpers import (
    format_discord_digest,
    infer_message_filter_symbol,
)
from app.config import get_settings
from app.services.agent_context_service import build_agent_context_from_cache
from app.services.options_playbook import (
    AgentModeKind,
    build_fast_summary_blob,
    build_playbook_context_blob,
    select_sections_for_context,
)
from app.services.llm_router import build_chat_openai, has_llm_provider

logger = logging.getLogger(__name__)

_STRATEGY_KEYWORDS = re.compile(
    r"策略|价差|spread|iron condor|straddle|strangle|风险|盈亏|credit|debit|卖方|买方",
    re.IGNORECASE,
)


class UserAgentState(TypedDict, total=False):
    question: str
    ticker_hint: str
    resolved_ticker: str
    discord_context: str
    market_bundle: str
    answer: str


def _safe_json(obj: object) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        return "{}"


def _playbook_mode_for_question(question: str) -> AgentModeKind:
    if _STRATEGY_KEYWORDS.search(question):
        return "strategy"
    if len(question.strip()) > 80:
        return "analysis"
    return "fast"


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
    bundle = build_agent_context_from_cache(ticker)
    return {"market_bundle": _safe_json(bundle)}


def synthesize_llm_answer(state: UserAgentState) -> dict[str, str]:
    cfg = get_settings()
    if not has_llm_provider(cfg):
        return {"answer": "服务端未配置 LLM Provider，无法调用语言模型。"}

    llm = build_chat_openai(
        cfg,
        openrouter_model=cfg.model_synthesis,
        source="user_agent",
        temperature=0.25,
        timeout=120,
        max_retries=2,
    )

    question = state.get("question", "").strip()
    ticker = state.get("resolved_ticker") or "SPY"

    base_prompt = (
        "你是美股期权与市场结构分析师 OptionsAji。必须用中文作答。\n"
        "参考提供的市场数据和 Discord 存档；不自造成交价。\n"
        "风险提示：教育是目的，不构成投资建议。\n"
    )

    unified_instructions = (
        "根据用户问题自动调整回答深度，无需用户选择模式：\n"
        "- 简单事实类（价格、IV、GEX 数值）→ 3-5 句简洁回答\n"
        "- 分析类（环境评估、趋势、多维度解读）→ 结构化分析：市场环境 / 期权数据 / 风险点\n"
        "- 策略/情景类（价差、风险收益）→ 结构说明 + 最大盈亏 + Greeks，附教育性免责声明\n\n"
        "涉及 GEX 关键指标时，可在结尾附 JSON cards block：\n"
        '```json\n{"cards":{"items":[{"label":"Net GEX","value":"$2.4B","color":"text-green"},{"label":"Gamma Flip","value":"$525","color":"text-foreground"}]}}\n```\n'
        "涉及期权结构对比时，可附 JSON table block：\n"
        '```json\n{"table":{"headers":["结构","构成","最大收益","最大亏损","盈亏平衡"],"rows":[["Bull Call Spread","买入 $740C + 卖出 $750C","$10/share","$2.50/share","$742.50"]]}}\n```\n'
        "始终引用提供的缓存/库内数据，不自造价格；若某字段缺失应明确告知用户。\n"
    )

    sys_prompt = base_prompt + unified_instructions

    playbook_mode = _playbook_mode_for_question(question)
    if playbook_mode == "fast":
        playbook_blob = build_fast_summary_blob()
    else:
        section_ids = select_sections_for_context(mode=playbook_mode)
        playbook_blob = build_playbook_context_blob(section_ids)
    if playbook_blob:
        sys_prompt += (
            "\n\n以下为期权内训教材摘录，回答时可引用 DTE、IV Rank、Greeks、Expected Move、异动五步法，"
            "勿照搬为投资建议：\n"
            f"{playbook_blob}\n"
        )

    human = HumanMessage(
        content=(
            f"用户提问：{question}\n"
            f"主要标的代码：{ticker}\n"
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
    *, question: str, ticker: Optional[str]
) -> UserAgentState:
    guard = question.strip()
    ticker_hint = (ticker or "").strip()
    return {"question": guard, "ticker_hint": ticker_hint}


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
    *, question: str, ticker: Optional[str]
) -> UserAgentState:
    initial = build_initial_agent_state(question=question, ticker=ticker)
    return execute_user_agent_pipeline(initial)
