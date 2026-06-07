"""User agent SSE."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from typing import Optional

from fastapi import APIRouter, Depends

from pydantic import BaseModel, Field
from starlette.responses import StreamingResponse

from app.agents.user_agent import (
    UserAgentState,
    build_initial_agent_state,
    fetch_market_bundle,
    gather_discord_snapshot,
    synthesize_llm_answer,
)
from app.api.billing_access import ensure_agent_billing
from app.config import get_settings
from app.services.social_sentiment import build_smart_vs_retail

router = APIRouter(tags=["agent"])
AGENT_SSE_HEARTBEAT_SECONDS = 10.0


class AgentQueryPayload(BaseModel):
    question: str = Field(min_length=1, max_length=8000)
    ticker: Optional[str] = Field(default=None, max_length=12)
    session_id: Optional[str] = Field(default=None, max_length=64)
    mode: Optional[str] = Field(default=None, max_length=16)


class PlanStep(BaseModel):
    id: str
    title: str
    owner: str


def _sse_pack(obj: dict[str, object]) -> bytes:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8")


def _event_payload(kind: str, content: str, **extras: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "type": kind,
        "content": content,
        "ts_unix_ms": int(time.time() * 1000),
    }
    payload.update(extras)
    return payload


def _build_plan() -> list[PlanStep]:
    return [
        PlanStep(id="sentiment", title="读取社媒情绪与共振快照", owner="sentiment_analyst"),
        PlanStep(id="market", title="从缓存加载行情、期权链与 GEX", owner="options_flow_analyst"),
        PlanStep(id="synthesis", title="综合结论并生成回答", owner="synthesis_agent"),
    ]


async def _run_thread_with_heartbeats(
    func: Callable[..., object],
    *args: object,
    heartbeat_content: str,
) -> AsyncIterator[tuple[str, object]]:
    task = asyncio.create_task(asyncio.to_thread(func, *args))
    while True:
        done, _ = await asyncio.wait({task}, timeout=AGENT_SSE_HEARTBEAT_SECONDS)
        if task in done:
            yield "result", task.result()
            return
        yield "heartbeat", _event_payload("thinking", heartbeat_content)


@router.post("/api/agent/query")
async def agent_query_stream(
    body: AgentQueryPayload,
    _: str = Depends(ensure_agent_billing),
) -> StreamingResponse:
    """SSE 流：`thinking` → `data_fetched` → `answer` → `done`。"""
    cfg = get_settings()
    if not cfg.feature_deep_agent_enabled:
        async def disabled_stream() -> AsyncIterator[bytes]:
            yield _sse_pack({"type": "answer", "content": "Deep Agent 功能暂未开启。"})
            yield _sse_pack({"type": "done"})
        return StreamingResponse(disabled_stream(), media_type="text/event-stream")

    ticker_str = None if body.ticker is None else body.ticker.upper()

    async def gen_bytes() -> AsyncIterator[bytes]:
        plan_steps = _build_plan()
        yield _sse_pack(
            _event_payload(
                "planning",
                f"已生成执行计划，共 {len(plan_steps)} 步。",
                plan=[step.model_dump() for step in plan_steps],
            ),
        )
        yield _sse_pack(
            _event_payload(
                "thinking",
                "开始分析您的问题，并准备载入 Discord 存档上下文。",
            ),
        )
        try:
            initial = build_initial_agent_state(
                question=body.question,
                ticker=ticker_str,
            )
            if not initial.get("question", "").strip():
                yield _sse_pack(_event_payload("error", "问题不能为空。"))
                yield _sse_pack({"type": "done"})
                return

            gather_delta = await asyncio.to_thread(
                gather_discord_snapshot,
                initial,  # type: ignore[arg-type]
            )
            state: UserAgentState = {**initial, **gather_delta}
            sym_g = str(state.get("resolved_ticker", ticker_str or "SPY"))
            yield _sse_pack(
                _event_payload(
                    "subagent_start",
                    f"sentiment_analyst 正在处理 {sym_g} 社媒情绪。",
                    agent="sentiment_analyst",
                )
            )
            social_snapshot = await asyncio.to_thread(build_smart_vs_retail, sym_g)
            yield _sse_pack(
                _event_payload(
                    "subagent_done",
                    (
                        f"sentiment_analyst 完成：散户 {social_snapshot.retail_direction}"
                        f" {social_snapshot.retail_sentiment_score}/100。"
                    ),
                    agent="sentiment_analyst",
                )
            )
            ctx = (state.get("discord_context") or "").strip()
            ctx_note = f"已拼接 {len(ctx)} 字符。" if ctx else "当前无匹配存档片段。"
            yield _sse_pack(
                _event_payload(
                    "thinking",
                    f"阶段 1/3：Discord 摘要完成，聚焦标的 {sym_g}；{ctx_note}",
                ),
            )

            yield _sse_pack(
                _event_payload(
                    "subagent_start",
                    f"options_flow_analyst 正在从缓存加载 {sym_g} 行情与期权数据…",
                    agent="options_flow_analyst",
                ),
            )

            fetch_delta = None
            async for item_type, item in _run_thread_with_heartbeats(
                fetch_market_bundle,
                state,  # type: ignore[arg-type]
                heartbeat_content=f"options_flow_analyst 仍在从缓存加载 {sym_g} 数据，请稍候。",
            ):
                if item_type == "heartbeat":
                    yield _sse_pack(item)  # type: ignore[arg-type]
                else:
                    fetch_delta = item
            if not isinstance(fetch_delta, dict):
                raise TypeError("market_fetch_result_not_dict")
            state = {**state, **fetch_delta}
            mb = state.get("market_bundle") or "{}"
            mb_len = len(mb)
            symbol = str(state.get("resolved_ticker", sym_g))

            yield _sse_pack(
                _event_payload(
                    "subagent_done",
                    f"options_flow_analyst 完成：缓存数据快照 {mb_len} 字符。",
                    agent="options_flow_analyst",
                ),
            )
            yield _sse_pack(
                _event_payload(
                    "data_fetched",
                    f"已从缓存加载 {symbol} 数据，JSON 约 {mb_len} 字符。",
                    resolved_ticker=symbol,
                    session_id=body.session_id,
                ),
            )

            yield _sse_pack(
                _event_payload(
                    "subagent_start",
                    "synthesis_agent 正在综合 Discord、期权、情绪数据。",
                    agent="synthesis_agent",
                ),
            )

            synth_delta = None
            async for item_type, item in _run_thread_with_heartbeats(
                synthesize_llm_answer,
                state,  # type: ignore[arg-type]
                heartbeat_content="synthesis_agent 仍在综合数据并生成回答，请稍候。",
            ):
                if item_type == "heartbeat":
                    yield _sse_pack(item)  # type: ignore[arg-type]
                else:
                    synth_delta = item
            if not isinstance(synth_delta, dict):
                raise TypeError("synthesis_result_not_dict")
            state = {**state, **synth_delta}

            ans_raw = state.get("answer")
            ans = ans_raw if isinstance(ans_raw, str) else str(ans_raw)
            yield _sse_pack(
                _event_payload(
                    "subagent_done",
                    "synthesis_agent 已生成最终回答。",
                    agent="synthesis_agent",
                )
            )
            yield _sse_pack({"type": "answer", "content": ans})
        except Exception as exc:
            yield _sse_pack({"type": "error", "content": f"{type(exc).__name__}: {exc!s}"})
            yield _sse_pack({"type": "done"})
            return

        yield _sse_pack({"type": "done"})

    return StreamingResponse(gen_bytes(), media_type="text/event-stream")
