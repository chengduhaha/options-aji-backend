"""Ingest and upsert helpers for the supply-chain graph."""
from __future__ import annotations

import datetime as dt
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import and_, bindparam, func, or_, select, text
from sqlalchemy.orm import Session

from app.db.models import GraphEdgeRow, GraphNodeRow, GraphViewRow
from app.graph.constants import GRAPH_INGEST_SCHEMA
from app.services.cache_service import TTL_WARM, cache_delete_pattern, cache_get, cache_set

_validator = Draft202012Validator(GRAPH_INGEST_SCHEMA, format_checker=FormatChecker())


def validate_graph_payload(payload: dict[str, Any]) -> None:
    errors = sorted(_validator.iter_errors(payload), key=lambda e: list(e.path))
    if errors:
        first = errors[0]
        path = ".".join(str(p) for p in first.path)
        location = f" at {path}" if path else ""
        raise ValueError(f"schema validation failed{location}: {first.message}")


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _norm_ticker(value: Any) -> str | None:
    text = _clean_str(value)
    return text.upper() if text else None


def _norm_market(value: Any) -> str | None:
    text = _clean_str(value)
    return text.upper() if text else None


def _parse_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    return dt.date.fromisoformat(value[:10])


def _node_lookup_stmt(row: dict[str, Any]):
    ticker = _norm_ticker(row.get("ticker"))
    market = _norm_market(row.get("market"))
    if ticker:
        return select(GraphNodeRow).where(
            GraphNodeRow.ticker == ticker,
            GraphNodeRow.market == market,
        )
    name_en = _clean_str(row.get("name_en"))
    name_zh = _clean_str(row.get("name_zh")) or ""
    node_type = str(row.get("node_type"))
    if name_en:
        return select(GraphNodeRow).where(
            GraphNodeRow.node_type == node_type,
            GraphNodeRow.name_en == name_en,
            GraphNodeRow.ticker.is_(None),
        )
    return select(GraphNodeRow).where(
        GraphNodeRow.node_type == node_type,
        GraphNodeRow.name_zh == name_zh,
        GraphNodeRow.ticker.is_(None),
    )


def _upsert_node(session: Session, row: dict[str, Any]) -> GraphNodeRow:
    existing = session.execute(_node_lookup_stmt(row).limit(1)).scalar_one_or_none()
    attrs = row.get("attrs") if isinstance(row.get("attrs"), dict) else {}
    values = {
        "node_type": str(row["node_type"]),
        "ticker": _norm_ticker(row.get("ticker")),
        "market": _norm_market(row.get("market")),
        "name_zh": str(row["name_zh"]).strip(),
        "name_en": _clean_str(row.get("name_en")),
        "sector": _clean_str(row.get("sector")),
        "is_listed": bool(row.get("is_listed", False)),
        "logo_url": _clean_str(row.get("logo_url")),
        "attrs": attrs,
    }
    if existing is None:
        node = GraphNodeRow(**values)
        session.add(node)
        session.flush()
        return node
    for key, value in values.items():
        setattr(existing, key, value)
    session.flush()
    return existing


def _edge_lookup_stmt(values: dict[str, Any]):
    return select(GraphEdgeRow).where(
        and_(
            GraphEdgeRow.source_id == values["source_id"],
            GraphEdgeRow.target_id == values["target_id"],
            GraphEdgeRow.rel_type == values["rel_type"],
            GraphEdgeRow.label == values["label"],
            GraphEdgeRow.as_of_date == values["as_of_date"],
        )
    )


def _upsert_edge(
    session: Session,
    row: dict[str, Any],
    node_map: dict[str, GraphNodeRow],
) -> GraphEdgeRow:
    source_ref = str(row["source"])
    target_ref = str(row["target"])
    if source_ref not in node_map or target_ref not in node_map:
        raise ValueError(f"edge references unknown node: {source_ref}->{target_ref}")
    attrs = row.get("attrs") if isinstance(row.get("attrs"), dict) else {}
    values = {
        "source_id": node_map[source_ref].id,
        "target_id": node_map[target_ref].id,
        "rel_type": str(row["rel_type"]),
        "direction": str(row.get("direction") or "directed"),
        "label": _clean_str(row.get("label")),
        "semantic": _clean_str(row.get("semantic")),
        "moat_tier": _clean_str(row.get("moat_tier")),
        "weight": row.get("weight"),
        "attrs": attrs,
        "confidence": str(row.get("confidence") or "confirmed"),
        "evidence": _clean_str(row.get("evidence")),
        "source_url": _clean_str(row.get("source_url")),
        "as_of_date": _parse_date(str(row.get("as_of_date") or "")),
    }
    existing = session.execute(_edge_lookup_stmt(values).limit(1)).scalar_one_or_none()
    if existing is None:
        edge = GraphEdgeRow(**values)
        session.add(edge)
        session.flush()
        return edge
    for key, value in values.items():
        setattr(existing, key, value)
    session.flush()
    return existing


def _upsert_view(session: Session, row: dict[str, Any], node_map: dict[str, GraphNodeRow]) -> GraphViewRow:
    slug = str(row["slug"]).strip()
    focus_ref = _clean_str(row.get("focus"))
    focus_node_id = node_map[focus_ref].id if focus_ref and focus_ref in node_map else None
    values = {
        "slug": slug,
        "title": str(row["title"]).strip(),
        "perspective": str(row["perspective"]).strip(),
        "focus_node_id": focus_node_id,
        "description": _clean_str(row.get("description")),
        "config": row.get("config") if isinstance(row.get("config"), dict) else {},
    }
    existing = session.execute(select(GraphViewRow).where(GraphViewRow.slug == slug).limit(1)).scalar_one_or_none()
    if existing is None:
        view = GraphViewRow(**values)
        session.add(view)
        session.flush()
        return view
    for key, value in values.items():
        setattr(existing, key, value)
    session.flush()
    return existing


def ingest_graph_payload(session: Session, payload: dict[str, Any]) -> dict[str, int]:
    validate_graph_payload(payload)
    node_map: dict[str, GraphNodeRow] = {}
    for row in payload["nodes"]:
        node_map[str(row["id"])] = _upsert_node(session, row)
    edge_count = 0
    for row in payload.get("edges", []):
        _upsert_edge(session, row, node_map)
        edge_count += 1
    view_count = 0
    for row in payload.get("views", []):
        _upsert_view(session, row, node_map)
        view_count += 1
    session.commit()
    cache_delete_pattern("graph:*")
    return {
        "nodes_upserted": len(payload["nodes"]),
        "edges_upserted": edge_count,
        "views_upserted": view_count,
    }


def _serialize_node(node: GraphNodeRow) -> dict[str, Any]:
    return {
        "id": node.id,
        "type": node.node_type,
        "ticker": node.ticker,
        "market": node.market,
        "label": node.name_zh,
        "nameZh": node.name_zh,
        "nameEn": node.name_en,
        "sector": node.sector,
        "isListed": node.is_listed,
        "logo": node.logo_url,
        "metrics": node.attrs or {},
    }


def _serialize_edge(edge: GraphEdgeRow) -> dict[str, Any]:
    return {
        "id": edge.id,
        "source": edge.source_id,
        "target": edge.target_id,
        "relType": edge.rel_type,
        "direction": edge.direction,
        "label": edge.label,
        "semantic": edge.semantic,
        "moatTier": edge.moat_tier,
        "weight": edge.weight,
        "attrs": edge.attrs or {},
        "confidence": edge.confidence,
        "evidence": edge.evidence,
        "sourceUrl": edge.source_url,
        "asOfDate": edge.as_of_date.isoformat() if edge.as_of_date else None,
    }


def _serialize_view(view: GraphViewRow) -> dict[str, Any]:
    return {
        "id": view.id,
        "slug": view.slug,
        "title": view.title,
        "perspective": view.perspective,
        "focusNodeId": view.focus_node_id,
        "description": view.description,
        "config": view.config or {},
    }


def _normalize_list(values: list[str] | str | None) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        raw = values.split(",")
    else:
        raw = values
    return [str(v).strip() for v in raw if str(v).strip()]


def _find_focus_node(session: Session, focus: str) -> GraphNodeRow | None:
    value = focus.strip()
    if not value:
        return None
    upper = value.upper()
    lowered = value.lower()
    return session.execute(
        select(GraphNodeRow)
        .where(
            or_(
                GraphNodeRow.id == value,
                GraphNodeRow.ticker == upper,
                func.lower(GraphNodeRow.name_en) == lowered,
                func.lower(GraphNodeRow.name_zh) == lowered,
            )
        )
        .limit(1)
    ).scalar_one_or_none()


def _graph_cache_key(
    focus: str,
    perspective: str,
    depth: int,
    rel_types: list[str],
    moat_tier: str | None,
    as_of_date: dt.date | None,
) -> str:
    rel_key = ",".join(sorted(rel_types)) if rel_types else "*"
    moat_key = moat_tier or "*"
    date_key = as_of_date.isoformat() if as_of_date else "*"
    return f"graph:subgraph:{focus.upper()}:{perspective}:{depth}:{rel_key}:{moat_key}:{date_key}"


def _filtered_edge_stmt(
    node_ids: list[str],
    rel_types: list[str],
    moat_tier: str | None,
    as_of_date: dt.date | None,
):
    stmt = select(GraphEdgeRow).where(
        GraphEdgeRow.source_id.in_(node_ids),
        GraphEdgeRow.target_id.in_(node_ids),
    )
    if rel_types:
        stmt = stmt.where(GraphEdgeRow.rel_type.in_(rel_types))
    if moat_tier:
        stmt = stmt.where(GraphEdgeRow.moat_tier == moat_tier)
    if as_of_date:
        stmt = stmt.where(GraphEdgeRow.as_of_date <= as_of_date)
    return stmt


def _reachable_node_ids(
    session: Session,
    focus_id: str,
    depth: int,
    rel_types: list[str],
    moat_tier: str | None,
    as_of_date: dt.date | None,
    include_structural: bool = False,
) -> list[str]:
    traversal_rel_types = sorted({*rel_types, "has_segment"}) if include_structural and rel_types else rel_types
    rel_filter = "AND e.rel_type IN :rel_types" if traversal_rel_types else ""
    if moat_tier and include_structural:
        moat_filter = "AND (e.rel_type = 'has_segment' OR e.moat_tier = :moat_tier)"
    else:
        moat_filter = "AND e.moat_tier = :moat_tier" if moat_tier else ""
    date_filter = "AND e.as_of_date <= :as_of_date" if as_of_date else ""
    sql = text(
        f"""
        WITH RECURSIVE walk(node_id, depth) AS (
            SELECT CAST(:focus_id AS VARCHAR(36)) AS node_id, 0 AS depth
            UNION
            SELECT
                CASE WHEN e.source_id = walk.node_id THEN e.target_id ELSE e.source_id END AS node_id,
                walk.depth + 1 AS depth
            FROM graph_edges e
            JOIN walk ON e.source_id = walk.node_id OR e.target_id = walk.node_id
            WHERE walk.depth < :depth
              {rel_filter}
              {moat_filter}
              {date_filter}
        )
        SELECT node_id, MIN(depth) AS min_depth
        FROM walk
        GROUP BY node_id
        ORDER BY min_depth ASC, node_id ASC
        """
    )
    params: dict[str, Any] = {"focus_id": focus_id, "depth": max(0, depth)}
    if traversal_rel_types:
        sql = sql.bindparams(bindparam("rel_types", expanding=True))
        params["rel_types"] = traversal_rel_types
    if moat_tier:
        params["moat_tier"] = moat_tier
    if as_of_date:
        params["as_of_date"] = as_of_date
    return [str(row.node_id) for row in session.execute(sql, params)]


def get_graph_subgraph(
    session: Session,
    *,
    focus: str,
    perspective: str = "company",
    depth: int = 2,
    rel_types: list[str] | str | None = None,
    moat_tier: str | None = None,
    as_of_date: str | dt.date | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    normalized_rel_types = _normalize_list(rel_types)
    normalized_moat = _clean_str(moat_tier)
    replay_date = as_of_date if isinstance(as_of_date, dt.date) else _parse_date(as_of_date)
    safe_depth = max(0, min(int(depth), 4))
    cache_key = _graph_cache_key(focus, perspective, safe_depth, normalized_rel_types, normalized_moat, replay_date)
    if use_cache:
        cached = cache_get(cache_key)
        if isinstance(cached, dict):
            cached.setdefault("meta", {})["cache"] = "hit"
            return cached

    focus_node = _find_focus_node(session, focus)
    if focus_node is None:
        return {
            "nodes": [],
            "edges": [],
            "meta": {
                "perspective": perspective,
                "focus": focus,
                "depth": safe_depth,
                "filters": {"relTypes": normalized_rel_types, "moatTier": normalized_moat},
                "asOf": replay_date.isoformat() if replay_date else None,
                "cache": "miss",
            },
        }

    include_structural = perspective == "company"
    node_ids = _reachable_node_ids(
        session,
        focus_node.id,
        safe_depth,
        normalized_rel_types,
        normalized_moat,
        replay_date,
        include_structural=include_structural,
    )
    nodes = session.execute(select(GraphNodeRow).where(GraphNodeRow.id.in_(node_ids))).scalars().all()
    node_by_id = {node.id: node for node in nodes}
    ordered_nodes = [node_by_id[node_id] for node_id in node_ids if node_id in node_by_id]
    edges = session.execute(_filtered_edge_stmt(node_ids, normalized_rel_types, normalized_moat, replay_date)).scalars().all()
    as_of_dates = [edge.as_of_date for edge in edges if edge.as_of_date]
    result = {
        "nodes": [_serialize_node(node) for node in ordered_nodes],
        "edges": [_serialize_edge(edge) for edge in edges],
        "meta": {
            "perspective": perspective,
            "focus": focus_node.ticker or focus_node.name_en or focus_node.name_zh,
            "focusNodeId": focus_node.id,
            "depth": safe_depth,
            "filters": {"relTypes": normalized_rel_types, "moatTier": normalized_moat},
            "asOf": replay_date.isoformat() if replay_date else (max(as_of_dates).isoformat() if as_of_dates else None),
            "cache": "miss",
        },
    }
    if use_cache:
        cache_set(cache_key, result, ttl=TTL_WARM)
    return result


def search_graph(session: Session, q: str, limit: int = 10) -> list[dict[str, Any]]:
    query = q.strip()
    if not query:
        return []
    like = f"%{query.lower()}%"
    rows = session.execute(
        select(GraphNodeRow)
        .where(
            or_(
                func.lower(GraphNodeRow.ticker).like(like),
                func.lower(GraphNodeRow.name_zh).like(like),
                func.lower(GraphNodeRow.name_en).like(like),
                func.lower(GraphNodeRow.sector).like(like),
            )
        )
        .order_by(GraphNodeRow.is_listed.desc(), GraphNodeRow.ticker.is_(None), GraphNodeRow.name_zh)
        .limit(limit)
    ).scalars().all()
    return [_serialize_node(row) for row in rows]


def get_node_detail(session: Session, node_id: str) -> dict[str, Any] | None:
    node = session.get(GraphNodeRow, node_id)
    if node is None:
        return None
    edges = session.execute(
        select(GraphEdgeRow)
        .where(or_(GraphEdgeRow.source_id == node_id, GraphEdgeRow.target_id == node_id))
        .limit(100)
    ).scalars().all()
    neighbor_ids = sorted(
        {
            edge.source_id if edge.target_id == node_id else edge.target_id
            for edge in edges
        }
    )
    neighbors = session.execute(select(GraphNodeRow).where(GraphNodeRow.id.in_(neighbor_ids))).scalars().all()
    nodes = [node, *neighbors]
    return {
        "nodes": [_serialize_node(row) for row in nodes],
        "edges": [_serialize_edge(edge) for edge in edges],
        "meta": {"focusNodeId": node_id, "depth": 1, "count": {"nodes": len(nodes), "edges": len(edges)}},
    }


def list_industries(session: Session) -> dict[str, Any]:
    rows = session.execute(
        select(GraphNodeRow.sector, func.count(GraphNodeRow.id))
        .where(GraphNodeRow.sector.is_not(None))
        .group_by(GraphNodeRow.sector)
        .order_by(GraphNodeRow.sector)
    ).all()
    nodes = [
        {
            "id": str(sector),
            "type": "industry",
            "ticker": None,
            "market": None,
            "label": str(sector),
            "sector": str(sector),
            "metrics": {"companyCount": int(count)},
        }
        for sector, count in rows
    ]
    return {"nodes": nodes, "edges": [], "meta": {"count": len(nodes)}}


def list_graph_views(session: Session) -> dict[str, Any]:
    views = session.execute(select(GraphViewRow).order_by(GraphViewRow.title)).scalars().all()
    return {"nodes": [], "edges": [], "meta": {"count": len(views), "views": [_serialize_view(view) for view in views]}}


def save_graph_view(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    slug = _clean_str(payload.get("slug"))
    title = _clean_str(payload.get("title"))
    perspective = _clean_str(payload.get("perspective")) or "company"
    if not slug or not title:
        raise ValueError("slug and title are required")
    focus_ref = _clean_str(payload.get("focus"))
    focus_node = _find_focus_node(session, focus_ref) if focus_ref else None
    config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
    values = {
        "slug": slug,
        "title": title,
        "perspective": perspective,
        "focus_node_id": focus_node.id if focus_node else None,
        "description": _clean_str(payload.get("description")),
        "config": config,
    }
    existing = session.execute(select(GraphViewRow).where(GraphViewRow.slug == slug).limit(1)).scalar_one_or_none()
    if existing is None:
        view = GraphViewRow(**values)
        session.add(view)
        session.flush()
    else:
        view = existing
        for key, value in values.items():
            setattr(view, key, value)
        session.flush()
    session.commit()
    cache_delete_pattern("graph:views*")
    return _serialize_view(view)


def load_graph_view(session: Session, slug: str) -> dict[str, Any] | None:
    view = session.execute(select(GraphViewRow).where(GraphViewRow.slug == slug).limit(1)).scalar_one_or_none()
    if view is None:
        return None
    focus_node = session.get(GraphNodeRow, view.focus_node_id) if view.focus_node_id else None
    focus = focus_node.ticker or focus_node.id if focus_node else slug
    config = view.config or {}
    depth = int(config.get("default_depth") or config.get("depth") or 2)
    graph = get_graph_subgraph(
        session,
        focus=focus,
        perspective=view.perspective,
        depth=depth,
        rel_types=config.get("rel_types"),
        moat_tier=config.get("moat_tier"),
        as_of_date=config.get("as_of_date"),
    )
    graph["meta"]["view"] = _serialize_view(view)
    return graph


def list_graph_timeline(session: Session, focus: str, depth: int = 2) -> dict[str, Any]:
    graph = get_graph_subgraph(session, focus=focus, perspective="company", depth=depth, use_cache=False)
    node_ids = [str(node["id"]) for node in graph["nodes"]]
    if not node_ids:
        return {"nodes": [], "edges": [], "meta": {"focus": focus, "dates": []}}
    dates = session.execute(
        select(GraphEdgeRow.as_of_date)
        .where(
            GraphEdgeRow.as_of_date.is_not(None),
            GraphEdgeRow.source_id.in_(node_ids),
            GraphEdgeRow.target_id.in_(node_ids),
        )
        .distinct()
        .order_by(GraphEdgeRow.as_of_date)
    ).scalars().all()
    return {
        "nodes": [],
        "edges": [],
        "meta": {"focus": focus, "depth": depth, "dates": [date.isoformat() for date in dates if date]},
    }
