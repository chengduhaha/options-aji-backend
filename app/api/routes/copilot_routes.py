"""Copilot JSON agent (non-SSE); distinct from POST /api/agent/query SSE."""
from __future__ import annotations

import logging

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.cross_market.copilot_supervisor import build_supervisor
from app.cross_market.db_async import SessionLocal as OntologySessionLocal
from app.cross_market.ontology_registry import ontology
from app.cross_market.persistence import save_trace_record
from app.cross_market.trace_store import add_trace
from app.services.llm_router import configured_providers

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/copilot", tags=["copilot"])

_agent = None


def get_agent():
    global _agent
    if len(configured_providers()) > 1:
        return build_supervisor()
    if _agent is None:
        _agent = build_supervisor()
    return _agent


class CopilotQueryRequest(BaseModel):
    query: str = Field(min_length=1)
    trader_id: str = "demo_user"


@router.post("/query")
async def copilot_query(req: CopilotQueryRequest) -> dict:
    agent = get_agent()
    try:
        result = await agent.ainvoke({"messages": [{"role": "user", "content": req.query}]})
    except Exception as exc:
        logger.exception("copilot query failed")
        return {"response": "", "error": str(exc)[:500]}
    trace = add_trace(
        source="copilot.query",
        query=req.query,
        matched_pattern=ontology.match_pattern_for_trigger({"content": req.query}),
        used_objects=["Event", "Stock", "Signal", "CrossMarketArbitrage"],
        used_relations=["event_triggers_signal", "event_affects_stock", "event_has_arbitrage"],
    )
    if OntologySessionLocal is not None:
        try:
            async with OntologySessionLocal() as session:
                await save_trace_record(session, trace)
        except Exception:
            pass
    content = result["messages"][-1]["content"]
    return {"response": content}
