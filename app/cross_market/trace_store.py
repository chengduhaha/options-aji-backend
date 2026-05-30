"""In-memory ontology trace store for inspector fallback."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone

from pydantic import BaseModel, Field


class OntologyTrace(BaseModel):
    trace_id: str
    source: str
    query: str
    matched_pattern: str | None = None
    used_objects: list[str] = Field(default_factory=list)
    used_relations: list[str] = Field(default_factory=list)
    created_at: str


_TRACES: deque[OntologyTrace] = deque(maxlen=50)


def add_trace(
    source: str,
    query: str,
    matched_pattern: str | None,
    used_objects: list[str],
    used_relations: list[str],
) -> OntologyTrace:
    trace = OntologyTrace(
        trace_id=f"trace-{int(datetime.now(tz=timezone.utc).timestamp() * 1000)}",
        source=source,
        query=query,
        matched_pattern=matched_pattern,
        used_objects=used_objects,
        used_relations=used_relations,
        created_at=datetime.now(tz=timezone.utc).isoformat(),
    )
    _TRACES.appendleft(trace)
    return trace


def list_traces(limit: int = 20) -> list[OntologyTrace]:
    clean_limit = max(1, min(limit, 50))
    return list(_TRACES)[:clean_limit]
