"""MVP 个股期权合约筛选器 — DeepAgents 解读 + 规则兜底。"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.services.cache_service import TTL_AI, cache_get, cache_set
from app.services.llm_router import build_chat_openai, configured_providers, has_llm_provider
from app.services.mvp_market_agent import _rule_based_insights as _market_rule_based_insights
from app.services.mvp_market_context import build_mvp_market_context
from app.services.options_playbook import (
    build_playbook_context_blob,
    playbook_skill_path,
    rule_snippets_for_mvp,
    select_sections_for_context,
)

try:
    from deepagents import create_deep_agent
except ModuleNotFoundError:
    create_deep_agent = None

logger = logging.getLogger(__name__)

DirectionKind = Literal["bull", "bear"]
EngineKind = Literal["deepagents", "fallback", "rules"]

BUCKET_ZH = {
    "this_week": "本周到期",
    "next_week": "下周窗口",
    "monthly": "近月到期",
}


class ExpectedMoveRead(BaseModel):
    bucket: str
    bucket_zh: str
    interpretation: str


class StockOptionsInsightsPayload(BaseModel):
    framework_summary: str
    contracts_note: str
    expected_moves: list[ExpectedMoveRead] = Field(default_factory=list)
    combined_insight: str
    engine: EngineKind
    generated_at_utc: str
    cached: bool = False


class StockOptionsInsightRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=16)
    direction: DirectionKind
    spot: float | None = None
    iv_rank: float | None = None
    expected_moves: list[dict[str, Any]] = Field(default_factory=list)
    contracts: list[dict[str, Any]] = Field(default_factory=list)
    unusual_items: list[dict[str, Any]] = Field(default_factory=list)
    market_regime_code: str | None = None
    market_regime_label: str | None = None


def _five_minute_bucket_utc() -> str:
    now = datetime.now(timezone.utc)
    bucket_min = (now.minute // 5) * 5
    return now.replace(minute=bucket_min, second=0, microsecond=0).isoformat()


def _cache_key(req: StockOptionsInsightRequest) -> str:
    raw = json.dumps(
        {
            "bucket": _five_minute_bucket_utc(),
            "symbol": req.symbol.upper(),
            "direction": req.direction,
            "market_regime_code": req.market_regime_code,
            "market_regime_label": req.market_regime_label,
            "contracts": req.contracts[:8],
            "moves": req.expected_moves[:3],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return f"mvp:stock-options-insights:v1:{hash(raw)}"


def _parse_agent_json(content: str) -> dict[str, Any] | None:
    text = content.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
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


def _rule_based_insights(req: StockOptionsInsightRequest) -> StockOptionsInsightsPayload:
    sym = req.symbol.upper()
    side_zh = "看涨 Call" if req.direction == "bull" else "看跌 Put"
    n = len(req.contracts)

    framework = (
        f"「{sym}」期权合约筛选器：按你当前选择的{side_zh}方向，"
        f"从完整期权链中列出近月、流动性相对较好的合约（共 {n} 条），"
        "用于对比行权价与 IV，并非「异动期权」榜单。"
    )
    contracts_note = (
        "列表排序优先「剩余天数 DTE」较短，其次「当日成交量」较高。"
        "「量/持仓」= 今日成交量 / 未平仓量（OI），数字越大通常表示越容易成交、"
        "但 OI 高也可能是历史累积仓位，需结合价差与 IV 一起看。"
    )

    move_reads: list[ExpectedMoveRead] = []
    for row in req.expected_moves[:3]:
        bucket = str(row.get("bucket") or "")
        pct = row.get("pct")
        exp = str(row.get("expiration") or "")
        straddle = row.get("straddleUsd")
        bucket_zh = BUCKET_ZH.get(bucket, bucket)
        if bucket == "this_week":
            hint = f"本周窗口（到期日 {exp}）：ATM 跨式隐含约 ±{pct}% 价格波动（跨式约 ${straddle}）。"
        elif bucket == "next_week":
            hint = f"下周窗口（到期日 {exp}）：隐含波动略宽，约 ±{pct}%。"
        elif bucket == "monthly":
            hint = f"近月窗口（到期日 {exp}）：月度级隐含波动参考 ±{pct}%。"
        else:
            hint = f"到期 {exp}：隐含预期波动约 ±{pct}%。"
        move_reads.append(
            ExpectedMoveRead(bucket=bucket, bucket_zh=bucket_zh, interpretation=hint)
        )

    regime = req.market_regime_label or req.market_regime_code or "未提供"
    unusual_n = len(req.unusual_items)
    playbook_note = rule_snippets_for_mvp(req.iv_rank, unusual_n)
    combined = (
        f"综合：大盘环境「{regime}」；{sym} 现价约 {req.spot or '—'}，IV Rank {req.iv_rank or '—'}。"
        f"筛选器内合约为 {side_zh} 流动性筛选；"
        f"{'下方异动区另有 ' + str(unusual_n) + ' 条高评分合约可对照。' if unusual_n else '暂无异动条目对照。'}"
        f"【教材】{playbook_note} "
        "以上为期权市场结构描述，不构成交易建议。"
    )

    return StockOptionsInsightsPayload(
        framework_summary=framework,
        contracts_note=contracts_note,
        expected_moves=move_reads,
        combined_insight=combined,
        engine="rules",
        generated_at_utc=datetime.now(timezone.utc).isoformat(),
    )


def _backfill_market_regime(req: StockOptionsInsightRequest) -> StockOptionsInsightRequest:
    if req.market_regime_label or req.market_regime_code:
        return req
    try:
        market_insight = _market_rule_based_insights(build_mvp_market_context())
    except Exception as exc:
        logger.warning("stock options market regime backfill failed: %s", exc)
        return req
    return req.model_copy(
        update={
            "market_regime_code": market_insight.regime.code,
            "market_regime_label": market_insight.regime.label,
        }
    )


def _payload_from_parsed(parsed: dict[str, Any], engine: EngineKind) -> StockOptionsInsightsPayload | None:
    try:
        moves_raw = parsed.get("expected_moves")
        moves: list[ExpectedMoveRead] = []
        if isinstance(moves_raw, list):
            for row in moves_raw[:3]:
                if not isinstance(row, dict):
                    continue
                bucket = str(row.get("bucket") or "")
                moves.append(
                    ExpectedMoveRead(
                        bucket=bucket,
                        bucket_zh=str(row.get("bucket_zh") or BUCKET_ZH.get(bucket, bucket))[:40],
                        interpretation=str(row.get("interpretation") or "")[:300],
                    )
                )
        summary = str(parsed.get("framework_summary") or "").strip()
        contracts_note = str(parsed.get("contracts_note") or "").strip()
        combined = str(parsed.get("combined_insight") or "").strip()
        if not summary or not combined:
            return None
        return StockOptionsInsightsPayload(
            framework_summary=summary[:500],
            contracts_note=contracts_note[:400],
            expected_moves=moves,
            combined_insight=combined[:600],
            engine=engine,
            generated_at_utc=datetime.now(timezone.utc).isoformat(),
        )
    except Exception as exc:
        logger.warning("stock options insights parse failed: %s", exc)
        return None


_AGENT: Any = None


def _build_agent() -> Any:
    global _AGENT
    should_cache_agent = len(configured_providers()) <= 1
    if should_cache_agent and _AGENT is not None:
        return _AGENT

    model = build_chat_openai(
        openrouter_model=os.getenv("MVP_MARKET_MODEL", "").strip()
        or os.getenv("COPILOT_MODEL", "").strip()
        or None,
        xiaomi_model=os.getenv("MVP_XIAOMI_MODEL", "").strip() or None,
        source="mvp_stock_options",
        temperature=0.25,
    )
    instructions = """你是 OptionsAji 华语美股期权分析师，专门解读「期权合约筛选器」模块。

用户会提供：标的、上涨/下跌情景、筛选后的期权合约列表、Expected Move（this_week/next_week/monthly）、
可选的异动合约摘要、大盘 regime。

你必须只输出 JSON：
{
  "framework_summary": "用通俗中文说明「期权合约筛选器」在做什么（≤120字）",
  "contracts_note": "说明左侧合约表每一列含义，并澄清：来自完整期权链按方向+流动性筛选，不是异动榜（≤150字）",
  "expected_moves": [
    {"bucket":"this_week","bucket_zh":"本周到期","interpretation":"..."},
    {"bucket":"next_week","bucket_zh":"下周窗口","interpretation":"..."},
    {"bucket":"monthly","bucket_zh":"近月到期","interpretation":"..."}
  ],
  "combined_insight": "结合大盘 regime、IV、Expected Move、合约流动性做客观综合（≤200字）"
}

术语说明（写入解读时要用中文）：
- this_week = 0-6 日内最近到期的 ATM 跨式隐含波动
- next_week = 0-14 日窗口
- monthly = 近月（最长约半年内）到期
- 量/OI = 当日成交量 / 未平仓合约数
- Expected Move = ATM Call+Put 跨式价格推算的隐含价格波动幅度

约束：客观描述环境，禁止喊单、禁止承诺收益。
可引用内训教材（DTE、IV Rank、Expected Move、异动五步法）术语，仍须只输出上述 JSON。"""

    if create_deep_agent is None:
        from app.cross_market.copilot_supervisor import FallbackAgent

        agent = FallbackAgent(model=model, instructions=instructions)
        if should_cache_agent:
            _AGENT = agent
        return agent

    skill_dir = playbook_skill_path()
    skills_arg = [skill_dir] if Path(skill_dir).is_dir() else []
    agent = create_deep_agent(
        model=model,
        tools=[],
        system_prompt=instructions,
        skills=skills_arg if skills_arg else None,
    )
    if should_cache_agent:
        _AGENT = agent
    return agent


def get_cached_stock_options_insights(
    req: StockOptionsInsightRequest,
) -> StockOptionsInsightsPayload | None:
    cached = cache_get(_cache_key(req))
    if not isinstance(cached, dict):
        return None
    try:
        out = StockOptionsInsightsPayload.model_validate(cached)
        out.cached = True
        return out
    except Exception:
        return None


def generate_fast_stock_options_insights(
    req: StockOptionsInsightRequest,
) -> StockOptionsInsightsPayload:
    if not req.market_regime_label and not req.market_regime_code:
        req = req.model_copy(update={"market_regime_label": "等待市场总览"})
    return _rule_based_insights(req)


async def generate_stock_options_insights(
    req: StockOptionsInsightRequest,
) -> StockOptionsInsightsPayload:
    req = _backfill_market_regime(req)
    cache_key = _cache_key(req)
    cached = cache_get(cache_key)
    if isinstance(cached, dict):
        try:
            out = StockOptionsInsightsPayload.model_validate(cached)
            out.cached = True
            return out
        except Exception:
            pass

    if not has_llm_provider():
        return _rule_based_insights(req)

    agent = _build_agent()
    payload = req.model_dump()
    section_ids = select_sections_for_context(
        direction=req.direction,
        iv_rank=req.iv_rank,
        unusual_count=len(req.unusual_items),
        mode="mvp",
    )
    playbook_blob = build_playbook_context_blob(section_ids)
    prompt = "请解读以下期权合约筛选器快照并输出 JSON：\n\n" + json.dumps(
        payload, ensure_ascii=False
    )[:14000]
    if playbook_blob:
        prompt += "\n\n【内训教材摘录】\n" + playbook_blob[:10000]
    engine: EngineKind = "deepagents" if create_deep_agent is not None else "fallback"
    try:
        result = await agent.ainvoke({"messages": [{"role": "user", "content": prompt}]})
        content = _last_message_content(result)
        parsed = _parse_agent_json(content)
        if parsed:
            built = _payload_from_parsed(parsed, engine)
            if built:
                if not built.expected_moves:
                    built.expected_moves = _rule_based_insights(req).expected_moves
                cache_set(cache_key, built.model_dump(), ttl=TTL_AI)
                return built
    except Exception as exc:
        logger.warning("stock options insights agent failed: %s", exc)

    fallback = _rule_based_insights(req)
    cache_set(cache_key, fallback.model_dump(), ttl=min(TTL_AI, 300))
    return fallback
