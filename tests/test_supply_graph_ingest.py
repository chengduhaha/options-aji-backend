from __future__ import annotations

from typing import Generator

import json
import os
import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps_auth import get_current_admin_user
from app.db.models import Base, GraphEdgeRow, GraphNodeRow, GraphViewRow
from app.db.models_user import UserRow
from app.db.session import db_session_dep
from app.graph.service import (
    get_graph_bootstrap,
    get_graph_snapshot,
    get_graph_subgraph,
    ingest_graph_payload,
    save_graph_view,
    search_graph,
)


def _session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


def _sample_payload() -> dict:
    return {
        "nodes": [
            {
                "id": "spacex",
                "node_type": "company",
                "ticker": "SPCX",
                "market": "US",
                "name_zh": "SpaceX",
                "name_en": "SpaceX",
                "sector": "商业航天",
                "is_listed": True,
                "attrs": {"market_cap": 350000000000},
            },
            {
                "id": "anthropic",
                "node_type": "company",
                "ticker": None,
                "market": None,
                "name_zh": "Anthropic",
                "name_en": "Anthropic",
                "sector": "AI",
                "is_listed": False,
            },
            {
                "id": "filtronic",
                "node_type": "company",
                "ticker": "FTC",
                "market": "UK",
                "name_zh": "Filtronic",
                "name_en": "Filtronic",
                "sector": "卫星通信",
                "is_listed": True,
            },
            {
                "id": "stm",
                "node_type": "company",
                "ticker": "STM",
                "market": "EU",
                "name_zh": "意法半导体",
                "name_en": "STMicroelectronics",
                "sector": "半导体",
                "is_listed": True,
            },
        ],
        "edges": [
            {
                "source": "spacex",
                "target": "anthropic",
                "rel_type": "supplies_to",
                "direction": "directed",
                "label": "算力共享/转售",
                "semantic": "Anthropic 是 SpaceX 的下游算力客户。",
                "moat_tier": "primary",
                "weight": 0.9,
                "attrs": {"contract_value": "12.5 亿美元/月"},
                "confidence": "confirmed",
                "evidence": "SpaceX 2026 S-1 重组合并版",
                "source_url": "https://example.com/spacex-s1",
                "as_of_date": "2026-05-30",
            },
            {
                "source": "filtronic",
                "target": "spacex",
                "rel_type": "supplies_to",
                "direction": "directed",
                "label": "E-band 毫米波放大器",
                "semantic": "V3/V4 星载荷高频放大器主供。",
                "moat_tier": "exclusive",
                "weight": 0.95,
                "confidence": "confirmed",
                "evidence": "SpaceX 2026 S-1 重组合并版",
                "source_url": "https://example.com/spacex-s1",
                "as_of_date": "2026-05-30",
            },
        ],
    }


def test_ingest_graph_payload_upserts_nodes_by_ticker_market_and_edges_idempotently() -> None:
    SessionLocal = _session_factory()

    with SessionLocal() as session:
        first = ingest_graph_payload(session, _sample_payload())
        second = ingest_graph_payload(session, _sample_payload())

        nodes = session.execute(select(GraphNodeRow)).scalars().all()
        edges = session.execute(select(GraphEdgeRow)).scalars().all()

    assert first["nodes_upserted"] == 4
    assert second["nodes_upserted"] == 4
    assert len(nodes) == 4
    assert len(edges) == 2
    assert any(node.ticker == "STM" and node.market == "EU" for node in nodes)


def test_supply_chain_customer_direction_is_spacex_to_anthropic() -> None:
    SessionLocal = _session_factory()

    with SessionLocal() as session:
        ingest_graph_payload(session, _sample_payload())
        spacex = session.execute(select(GraphNodeRow).where(GraphNodeRow.ticker == "SPCX")).scalar_one()
        anthropic = session.execute(select(GraphNodeRow).where(GraphNodeRow.name_en == "Anthropic")).scalar_one()
        edge = session.execute(
            select(GraphEdgeRow).where(
                GraphEdgeRow.source_id == spacex.id,
                GraphEdgeRow.target_id == anthropic.id,
                GraphEdgeRow.rel_type == "supplies_to",
            )
        ).scalar_one()

    assert edge.label == "算力共享/转售"
    assert edge.direction == "directed"
    assert edge.confidence == "confirmed"
    assert edge.attrs["contract_value"] == "12.5 亿美元/月"


def test_ingest_rejects_unknown_relation_type() -> None:
    SessionLocal = _session_factory()
    payload = _sample_payload()
    payload["edges"][0]["rel_type"] = "unknown_rel"

    with SessionLocal() as session:
        try:
            ingest_graph_payload(session, payload)
        except ValueError as exc:
            assert "schema validation failed" in str(exc)
        else:
            raise AssertionError("unknown rel_type should be rejected")


def test_graph_ingest_route_accepts_normalized_json() -> None:
    from app.api.routes.supply_graph import router

    SessionLocal = _session_factory()
    app = FastAPI()

    def override_db() -> Generator[Session, None, None]:
        session = SessionLocal()
        try:
            yield session
        finally:
            session.close()

    async def override_admin() -> UserRow:
        return UserRow(email="admin@example.com", password_hash="x", role="admin")

    app.dependency_overrides[db_session_dep] = override_db
    app.dependency_overrides[get_current_admin_user] = override_admin
    app.include_router(router)

    client = TestClient(app)
    res = client.post("/api/v1/graph/ingest", json=_sample_payload())

    assert res.status_code == 200
    body = res.json()
    assert body["success"] is True
    assert body["data"]["nodes_upserted"] == 4
    assert body["data"]["edges_upserted"] == 2


def test_spacex_seed_script_is_repeatable(tmp_path: Path) -> None:
    db_path = tmp_path / "supply_graph_seed.db"
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{db_path}",
    }
    script = Path(__file__).resolve().parents[1] / "scripts" / "seed_supply_chain_graph.py"

    first = subprocess.run(
        [sys.executable, str(script)],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    second = subprocess.run(
        [sys.executable, str(script)],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert json.loads(first.stdout)["result"] == {
        "nodes_upserted": 26,
        "edges_upserted": 25,
        "views_upserted": 1,
    }
    assert json.loads(second.stdout)["result"] == {
        "nodes_upserted": 26,
        "edges_upserted": 25,
        "views_upserted": 1,
    }

    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with SessionLocal() as session:
        node_count = session.scalar(select(func.count()).select_from(GraphNodeRow))
        edge_count = session.scalar(select(func.count()).select_from(GraphEdgeRow))
        view_count = session.scalar(select(func.count()).select_from(GraphViewRow))
        inferred_count = session.scalar(
            select(func.count()).select_from(GraphEdgeRow).where(GraphEdgeRow.confidence == "inferred")
        )
        spacex = session.execute(select(GraphNodeRow).where(GraphNodeRow.ticker == "SPCX")).scalar_one()
        anthropic = session.execute(select(GraphNodeRow).where(GraphNodeRow.name_en == "Anthropic")).scalar_one()
        spacex_to_anthropic = session.execute(
            select(GraphEdgeRow).where(
                GraphEdgeRow.source_id == spacex.id,
                GraphEdgeRow.target_id == anthropic.id,
                GraphEdgeRow.rel_type == "supplies_to",
            )
        ).scalar_one()

    assert node_count == 26
    assert edge_count == 25
    assert view_count == 1
    assert inferred_count == 2
    assert spacex_to_anthropic.label == "算力共享/转售"


def test_company_perspective_depth_two_returns_segments_and_suppliers_from_spacex_seed() -> None:
    seed_path = Path(__file__).resolve().parents[1] / "scripts" / "spacex_supply_chain_2026_seed.json"
    payload = json.loads(seed_path.read_text(encoding="utf-8"))
    SessionLocal = _session_factory()

    with SessionLocal() as session:
        ingest_graph_payload(session, payload)
        graph = get_graph_subgraph(session, focus="SPCX", perspective="company", depth=2)

    labels = {node["label"] for node in graph["nodes"]}
    tickers = {node.get("ticker") for node in graph["nodes"]}
    edge_labels = {edge["label"] for edge in graph["edges"]}

    assert {"AI 分部", "Connectivity 分部", "Space 分部"}.issubset(labels)
    assert {"NVDA", "FTC", "HON", "DCO", "ATRO"}.issubset(tickers)
    assert "算力共享/转售" in edge_labels
    assert graph["meta"]["focus"] == "SPCX"
    assert graph["meta"]["perspective"] == "company"
    assert graph["meta"]["depth"] == 2


def test_graph_query_filters_by_relation_type_and_moat_tier() -> None:
    seed_path = Path(__file__).resolve().parents[1] / "scripts" / "spacex_supply_chain_2026_seed.json"
    payload = json.loads(seed_path.read_text(encoding="utf-8"))
    SessionLocal = _session_factory()

    with SessionLocal() as session:
        ingest_graph_payload(session, payload)
        graph = get_graph_subgraph(
            session,
            focus="SPCX",
            perspective="company",
            depth=2,
            rel_types=["supplies_to"],
            moat_tier="exclusive",
        )

    assert {edge["relType"] for edge in graph["edges"]} == {"supplies_to"}
    assert {edge["moatTier"] for edge in graph["edges"]} == {"exclusive"}
    assert {node.get("ticker") for node in graph["nodes"]}.issuperset({"SATS", "FTC", "347700", "MTRN"})


def test_graph_search_finds_company_by_ticker_and_name() -> None:
    SessionLocal = _session_factory()

    with SessionLocal() as session:
        ingest_graph_payload(session, _sample_payload())
        ticker_matches = search_graph(session, "spcx")
        name_matches = search_graph(session, "Anthropic")

    assert ticker_matches[0]["ticker"] == "SPCX"
    assert name_matches[0]["label"] == "Anthropic"


def test_graph_query_routes_return_uniform_graph_shape() -> None:
    from app.api.routes.supply_graph import router

    seed_path = Path(__file__).resolve().parents[1] / "scripts" / "spacex_supply_chain_2026_seed.json"
    payload = json.loads(seed_path.read_text(encoding="utf-8"))
    SessionLocal = _session_factory()
    app = FastAPI()

    def override_db() -> Generator[Session, None, None]:
        session = SessionLocal()
        try:
            yield session
        finally:
            session.close()

    async def override_admin() -> UserRow:
        return UserRow(email="admin@example.com", password_hash="x", role="admin")

    with SessionLocal() as session:
        ingest_graph_payload(session, payload)

    app.dependency_overrides[db_session_dep] = override_db
    app.dependency_overrides[get_current_admin_user] = override_admin
    app.include_router(router)

    client = TestClient(app)
    graph_res = client.get("/api/v1/graph", params={"focus": "SPCX", "perspective": "company", "depth": 2})
    node_id = graph_res.json()["nodes"][0]["id"]

    assert graph_res.status_code == 200
    assert set(graph_res.json()) == {"nodes", "edges", "meta"}
    assert client.get("/api/v1/graph/search", params={"q": "SpaceX"}).json()["nodes"][0]["ticker"] == "SPCX"
    assert set(client.get(f"/api/v1/graph/node/{node_id}").json()) == {"nodes", "edges", "meta"}
    assert client.get("/api/v1/graph/industries").json()["nodes"]
    assert client.get("/api/v1/graph/views").json()["meta"]["count"] == 1
    assert client.get("/api/v1/graph/views/spacex-2026-supply-chain").json()["meta"]["view"]["slug"] == "spacex-2026-supply-chain"
    timeline = client.get("/api/v1/graph/timeline", params={"focus": "SPCX", "depth": 2}).json()
    assert "2026-05-30" in timeline["meta"]["dates"]
    saved = client.post(
        "/api/v1/graph/views",
        json={
            "slug": "route-saved-view",
            "title": "Route Saved View",
            "perspective": "company",
            "focus": "SPCX",
            "config": {"default_depth": 2, "as_of_date": "2026-05-30"},
        },
    )
    assert saved.status_code == 200
    assert saved.json()["meta"]["view"]["slug"] == "route-saved-view"


def test_ingest_generates_curated_view_snapshot_and_snapshot_route(tmp_path: Path, monkeypatch) -> None:
    from app.api.routes.supply_graph import router

    seed_path = Path(__file__).resolve().parents[1] / "scripts" / "spacex_supply_chain_2026_seed.json"
    payload = json.loads(seed_path.read_text(encoding="utf-8"))
    SessionLocal = _session_factory()
    app = FastAPI()

    monkeypatch.setattr("app.graph.service.GRAPH_SNAPSHOT_DIR", tmp_path)

    def override_db() -> Generator[Session, None, None]:
        session = SessionLocal()
        try:
            yield session
        finally:
            session.close()

    async def override_admin() -> UserRow:
        return UserRow(email="admin@example.com", password_hash="x", role="admin")

    with SessionLocal() as session:
        ingest_graph_payload(session, payload)
        snapshot = get_graph_snapshot("spacex-2026-supply-chain")

    app.dependency_overrides[db_session_dep] = override_db
    app.dependency_overrides[get_current_admin_user] = override_admin
    app.include_router(router)
    client = TestClient(app)
    route_res = client.get("/api/v1/graph/snapshot/spacex-2026-supply-chain")

    assert (tmp_path / "spacex-2026-supply-chain.json").exists()
    assert snapshot is not None
    assert snapshot["meta"]["view"]["slug"] == "spacex-2026-supply-chain"
    assert len(snapshot["nodes"]) == 26
    assert route_res.status_code == 200
    assert route_res.headers["cache-control"] == "s-maxage=300, stale-while-revalidate=600"
    assert route_res.json()["meta"]["snapshot"] == "hit"


def test_graph_bootstrap_returns_graph_views_and_timeline() -> None:
    seed_path = Path(__file__).resolve().parents[1] / "scripts" / "spacex_supply_chain_2026_seed.json"
    payload = json.loads(seed_path.read_text(encoding="utf-8"))
    SessionLocal = _session_factory()

    with SessionLocal() as session:
        ingest_graph_payload(session, payload)
        bootstrap = get_graph_bootstrap(session, focus="SPCX", perspective="company", depth=2)

    assert set(bootstrap) == {"nodes", "edges", "meta"}
    assert len(bootstrap["nodes"]) == 26
    assert bootstrap["meta"]["views"][0]["slug"] == "spacex-2026-supply-chain"
    assert "2026-05-30" in bootstrap["meta"]["timelineDates"]
    assert bootstrap["meta"]["bootstrap"] is True


def test_read_graph_routes_send_swr_cache_headers() -> None:
    from app.api.routes.supply_graph import router

    seed_path = Path(__file__).resolve().parents[1] / "scripts" / "spacex_supply_chain_2026_seed.json"
    payload = json.loads(seed_path.read_text(encoding="utf-8"))
    SessionLocal = _session_factory()
    app = FastAPI()

    def override_db() -> Generator[Session, None, None]:
        session = SessionLocal()
        try:
            yield session
        finally:
            session.close()

    async def override_admin() -> UserRow:
        return UserRow(email="admin@example.com", password_hash="x", role="admin")

    with SessionLocal() as session:
        ingest_graph_payload(session, payload)

    app.dependency_overrides[db_session_dep] = override_db
    app.dependency_overrides[get_current_admin_user] = override_admin
    app.include_router(router)
    client = TestClient(app)

    graph_res = client.get("/api/v1/graph", params={"focus": "SPCX", "perspective": "company", "depth": 2})
    bootstrap_res = client.get("/api/v1/graph/bootstrap", params={"focus": "SPCX", "perspective": "company", "depth": 2})

    assert graph_res.headers["cache-control"] == "s-maxage=300, stale-while-revalidate=600"
    assert bootstrap_res.headers["cache-control"] == "s-maxage=300, stale-while-revalidate=600"
    assert bootstrap_res.json()["meta"]["bootstrap"] is True


def test_graph_subgraph_uses_cache_key_for_focus_depth_and_filters(monkeypatch) -> None:
    seed_path = Path(__file__).resolve().parents[1] / "scripts" / "spacex_supply_chain_2026_seed.json"
    payload = json.loads(seed_path.read_text(encoding="utf-8"))
    SessionLocal = _session_factory()
    cache: dict[str, dict] = {}

    def fake_get(key: str):
        return json.loads(json.dumps(cache[key])) if key in cache else None

    def fake_set(key: str, value: dict, ttl: int):
        cache[key] = json.loads(json.dumps(value))

    monkeypatch.setattr("app.graph.service.cache_get", fake_get)
    monkeypatch.setattr("app.graph.service.cache_set", fake_set)

    with SessionLocal() as session:
        ingest_graph_payload(session, payload)
        first = get_graph_subgraph(
            session,
            focus="SPCX",
            perspective="company",
            depth=2,
            rel_types=["supplies_to"],
            moat_tier="exclusive",
        )
        second = get_graph_subgraph(
            session,
            focus="SPCX",
            perspective="company",
            depth=2,
            rel_types=["supplies_to"],
            moat_tier="exclusive",
        )

    assert "graph:subgraph:SPCX:company:2:supplies_to:exclusive:*" in cache
    assert first["meta"]["cache"] == "miss"
    assert second["meta"]["cache"] == "hit"


def test_graph_subgraph_replays_edges_by_as_of_date() -> None:
    payload = _sample_payload()
    payload["edges"][1]["as_of_date"] = "2026-06-30"
    SessionLocal = _session_factory()

    with SessionLocal() as session:
        ingest_graph_payload(session, payload)
        may_graph = get_graph_subgraph(session, focus="SPCX", perspective="company", depth=2, as_of_date="2026-05-30")
        july_graph = get_graph_subgraph(session, focus="SPCX", perspective="company", depth=2, as_of_date="2026-07-01")

    may_tickers = {node.get("ticker") for node in may_graph["nodes"]}
    july_tickers = {node.get("ticker") for node in july_graph["nodes"]}
    assert "FTC" not in may_tickers
    assert "FTC" in july_tickers
    assert may_graph["meta"]["asOf"] == "2026-05-30"


def test_graph_view_can_be_saved_and_loaded_with_query_config() -> None:
    seed_path = Path(__file__).resolve().parents[1] / "scripts" / "spacex_supply_chain_2026_seed.json"
    payload = json.loads(seed_path.read_text(encoding="utf-8"))
    SessionLocal = _session_factory()

    with SessionLocal() as session:
        ingest_graph_payload(session, payload)
        view = save_graph_view(
            session,
            {
                "slug": "spacex-ai-exclusive",
                "title": "SpaceX AI 独家供应链",
                "perspective": "company",
                "focus": "SPCX",
                "description": "只看 AI 分部的高护城河链路。",
                "config": {
                    "default_depth": 2,
                    "rel_types": ["supplies_to"],
                    "moat_tier": "exclusive",
                    "business_segment": "segment_ai",
                    "as_of_date": "2026-05-30",
                },
            },
        )
        loaded = session.execute(select(GraphViewRow).where(GraphViewRow.slug == "spacex-ai-exclusive")).scalar_one()

    assert view["slug"] == "spacex-ai-exclusive"
    assert loaded.title == "SpaceX AI 独家供应链"
    assert loaded.config["moat_tier"] == "exclusive"
