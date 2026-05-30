"""Ontology definitions + inspector API."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.cross_market.db_async import SessionLocal as OntologySessionLocal
from app.cross_market.ontology_registry import ontology
from app.cross_market.persistence import list_recent_trace_records
from app.cross_market.trace_store import OntologyTrace, list_traces

objects_router = APIRouter(prefix="/api/ontology", tags=["ontology"])


@objects_router.get("/objects")
async def list_objects() -> dict:
    return {"objects": ontology.list_objects()}


@objects_router.get("/objects/{name}")
async def get_object(name: str) -> dict:
    try:
        return ontology.load_object(name).model_dump()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@objects_router.get("/patterns")
async def list_patterns(type: Optional[str] = None) -> dict:
    return {"patterns": ontology.list_patterns(type)}


@objects_router.get("/patterns/{pattern_id}")
async def get_pattern(pattern_id: str) -> dict:
    try:
        return ontology.load_pattern(pattern_id).model_dump()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


inspector_router = APIRouter(prefix="/api/ontology/inspector", tags=["ontology"])


class InspectorResponse(BaseModel):
    objects: list[str]
    relations: list[str]
    patterns: list[str]
    recent_traces: list[OntologyTrace]


@inspector_router.get("", response_model=InspectorResponse)
async def get_inspector(limit: int = Query(default=20, ge=1, le=50)) -> InspectorResponse:
    db_traces: list[OntologyTrace] = []
    if OntologySessionLocal is not None:
        try:
            async with OntologySessionLocal() as session:
                db_traces = await list_recent_trace_records(session, limit=limit)
        except Exception:
            db_traces = []
    traces = db_traces if db_traces else list_traces(limit=limit)
    return InspectorResponse(
        objects=ontology.list_objects(),
        relations=ontology.list_relations(),
        patterns=ontology.list_patterns(),
        recent_traces=traces,
    )
