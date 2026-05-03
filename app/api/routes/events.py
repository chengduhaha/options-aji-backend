"""SSE market alerts stub + fan-out subscriber."""

from __future__ import annotations

import asyncio
import datetime as dt
import json

from typing import AsyncIterator, Optional

from fastapi import APIRouter, Depends
from starlette.responses import StreamingResponse

from app.api.deps import bearer_subscription_optional
from app.events.broadcaster import broker

router = APIRouter(tags=["events"])


@router.get("/api/events/stream")
async def sse_market_feed(
    _: Optional[str] = Depends(bearer_subscription_optional),
) -> StreamingResponse:
    """Subscribe asynchronous market alerts."""

    heartbeat_sec = 25.0
    inbound = await broker.subscribe()

    async def gen_bytes() -> AsyncIterator[bytes]:
        try:
            while True:
                try:
                    item = await asyncio.wait_for(inbound.get(), timeout=heartbeat_sec)
                except asyncio.TimeoutError:
                    item = {
                        "type": "heartbeat",
                        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
                    }

                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n".encode("utf-8")
        finally:
            await broker.unsubscribe(inbound)

    return StreamingResponse(gen_bytes(), media_type="text/event-stream")
