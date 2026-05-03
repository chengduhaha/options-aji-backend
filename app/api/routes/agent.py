"""User agent SSE."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Optional

from fastapi import APIRouter, Depends

from pydantic import BaseModel, Field
from starlette.responses import StreamingResponse

from app.agents.user_agent import run_user_agent_once
from app.api.deps import bearer_subscription_optional

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
    _: Optional[str] = Depends(bearer_subscription_optional),
) -> StreamingResponse:
    """SSE 流：`thinking` → `data_fetched` → `answer` → `done`。"""

    ticker_str = None if body.ticker is None else body.ticker.upper()

    async def gen_bytes() -> AsyncIterator[bytes]:
        yield _sse_pack({"type": "thinking", "content": "正在解析问题并拉取市场行情…"})
        try:
            result = await asyncio.to_thread(
                run_user_agent_once,
                question=body.question,
                ticker=ticker_str,
            )
        except Exception as exc:
            yield _sse_pack({"type": "error", "content": f"{type(exc).__name__}: {exc!s}"})
            yield _sse_pack({"type": "done"})
            return

        symbol = str(result.get("resolved_ticker", ticker_str or "SPY"))
        yield _sse_pack(
            {
                "type": "data_fetched",
                "content": f"已获取 {symbol} 的行情与期权链快照。",
                "resolved_ticker": symbol,
                "session_id": body.session_id,
            },
        )

        ans_raw = result.get("answer")
        ans = ans_raw if isinstance(ans_raw, str) else str(ans_raw)
        yield _sse_pack({"type": "answer", "content": ans})

        yield _sse_pack({"type": "done"})

    return StreamingResponse(gen_bytes(), media_type="text/event-stream")
