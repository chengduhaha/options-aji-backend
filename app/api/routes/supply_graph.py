"""Supply-chain graph API routes."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps_auth import get_current_admin_user
from app.db.models_user import UserRow
from app.db.session import db_session_dep
from app.graph.service import (
    get_graph_subgraph,
    get_node_detail,
    ingest_graph_payload,
    list_graph_views,
    list_graph_timeline,
    list_industries,
    load_graph_view,
    save_graph_view,
    search_graph,
)

router = APIRouter(prefix="/api/v1/graph", tags=["supply-graph"])


@router.get("/search")
def search_graph_entities(
    q: str,
    limit: int = 10,
    session: Session = Depends(db_session_dep),
) -> dict[str, object]:
    nodes = search_graph(session, q, limit=min(max(limit, 1), 25))
    return {"nodes": nodes, "edges": [], "meta": {"query": q, "count": len(nodes)}}


@router.get("/node/{node_id}")
def get_graph_node(node_id: str, session: Session = Depends(db_session_dep)) -> dict[str, object]:
    result = get_node_detail(session, node_id)
    if result is None:
        raise HTTPException(status_code=404, detail={"code": "graph_node_not_found", "message": node_id})
    return result


@router.get("/industries")
def get_graph_industries(session: Session = Depends(db_session_dep)) -> dict[str, object]:
    return list_industries(session)


@router.get("/views")
def get_graph_views(session: Session = Depends(db_session_dep)) -> dict[str, object]:
    return list_graph_views(session)


@router.post("/views")
def save_curated_graph_view(
    payload: dict[str, Any],
    session: Session = Depends(db_session_dep),
    _: UserRow = Depends(get_current_admin_user),
) -> dict[str, object]:
    try:
        view = save_graph_view(session, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "invalid_graph_view", "message": str(exc)}) from exc
    return {"nodes": [], "edges": [], "meta": {"view": view}}


@router.get("/views/{slug}")
def get_graph_view(slug: str, session: Session = Depends(db_session_dep)) -> dict[str, object]:
    result = load_graph_view(session, slug)
    if result is None:
        raise HTTPException(status_code=404, detail={"code": "graph_view_not_found", "message": slug})
    return result


@router.get("/timeline")
def get_graph_timeline(
    focus: str,
    depth: int = 2,
    session: Session = Depends(db_session_dep),
) -> dict[str, object]:
    return list_graph_timeline(session, focus=focus, depth=depth)


@router.get("")
def query_graph(
    focus: str,
    perspective: str = "company",
    depth: int = 2,
    rel_types: str | None = None,
    moat_tier: str | None = None,
    as_of_date: str | None = None,
    session: Session = Depends(db_session_dep),
) -> dict[str, object]:
    return get_graph_subgraph(
        session,
        focus=focus,
        perspective=perspective,
        depth=depth,
        rel_types=rel_types,
        moat_tier=moat_tier,
        as_of_date=as_of_date,
    )


@router.post("/ingest")
def ingest_graph(
    payload: dict[str, Any],
    session: Session = Depends(db_session_dep),
    _: UserRow = Depends(get_current_admin_user),
) -> dict[str, object]:
    try:
        result = ingest_graph_payload(session, payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_graph_payload", "message": str(exc)},
        ) from exc
    return {"success": True, "data": result}
