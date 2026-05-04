"""User agent SSE."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
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

router = APIRouter(tags=["agent"])


class AgentQueryPayload(BaseModel):
    question: str = Field(min_length=1, max_length=8000)
    ticker: Optional[str] = Field(default=None, max_length=12)
    session_id: Optional[str] = Field(default=None, max_length=64)


def _sse_pack(obj: dict[str, object]) -> bytes:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8")


@router.post("/api/agent/query")
async def agent_query_stream(
    body: AgentQueryPayload,
    _: str = Depends(ensure_agent_billing),
) -> StreamingResponse:
    """SSE 流：`thinking` → `data_fetched` → `answer` → `done`。"""

    ticker_str = None if body.ticker is None else body.ticker.upper()

    async def gen_bytes() -> AsyncIterator[bytes]:
        yield _sse_pack(
            {
                "type": "thinking",
                "content": "开始处理：解析提问与标的提示，并准备载入 Discord 存档上下文。",
            },
        )
        try:
            initial = build_initial_agent_state(
                question=body.question,
                ticker=ticker_str,
            )
            if not initial.get("question", "").strip():
                yield _sse_pack({"type": "error", "content": "问题不能为空。"})
                yield _sse_pack({"type": "done"})
                return

            gather_delta = await asyncio.to_thread(
                gather_discord_snapshot,
                initial,  # type: ignore[arg-type]
            )
            state: UserAgentState = {**initial, **gather_delta}
            sym_g = str(state.get("resolved_ticker", ticker_str or "SPY"))
            ctx = (state.get("discord_context") or "").strip()
            ctx_note = f"已拼接 {len(ctx)} 字符。" if ctx else "当前无匹配存档片段。"
            yield _sse_pack(
                {
                    "type": "thinking",
                    "content": f"阶段 1/3：Discord 摘要完成，聚焦标的 {sym_g}；{ctx_note}",
                },
            )

            yield _sse_pack(
                {
                    "type": "thinking",
                    "content": f"阶段 2/3：拉取 {sym_g} 行情、期权链与（若配置的）上游 GEX 摘要…",
                },
            )

            fetch_delta = await asyncio.to_thread(
                fetch_market_bundle,
                state,  # type: ignore[arg-type]
            )
            state = {**state, **fetch_delta}
            mb = state.get("market_bundle") or "{}"
            mb_len = len(mb)
            symbol = str(state.get("resolved_ticker", sym_g))

            yield _sse_pack(
                {
                    "type": "data_fetched",
                    "content": (
                        f"市场数据快照已就绪（{symbol}），JSON 约 {mb_len} 字符。"
                    ),
                    "resolved_ticker": symbol,
                    "session_id": body.session_id,
                },
            )

            yield _sse_pack(
                {
                    "type": "thinking",
                    "content": "阶段 3/3：调用语言模型综合 Discord 与市场 JSON；未配置 OPENROUTER_API_KEY 时将返回离线说明。",
                },
            )

            synth_delta = await asyncio.to_thread(
                synthesize_llm_answer,
                state,  # type: ignore[arg-type]
            )
            state = {**state, **synth_delta}

            ans_raw = state.get("answer")
            ans = ans_raw if isinstance(ans_raw, str) else str(ans_raw)
            yield _sse_pack({"type": "answer", "content": ans})
        except Exception as exc:
            yield _sse_pack({"type": "error", "content": f"{type(exc).__name__}: {exc!s}"})
            yield _sse_pack({"type": "done"})
            return

        yield _sse_pack({"type": "done"})

    return StreamingResponse(gen_bytes(), media_type="text/event-stream")
