"""Integration-style tests for agent SSE stream contract."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.billing_access import ensure_agent_billing
from app.api.routes import agent as agent_route
from app.api.routes.agent import router as agent_router


def _build_client() -> TestClient:
    app = FastAPI()
    app.include_router(agent_router)
    app.dependency_overrides[ensure_agent_billing] = lambda: "ok-token"
    return TestClient(app)


def _parse_sse_payloads(raw_text: str) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for block in raw_text.split("\n\n"):
        line = block.strip()
        if not line.startswith("data:"):
            continue
        body = line.removeprefix("data:").strip()
        if not body:
            continue
        payloads.append(json.loads(body))
    return payloads


def test_agent_sse_events_include_timestamp(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        agent_route,
        "get_settings",
        lambda: SimpleNamespace(feature_deep_agent_enabled=True),
    )
    monkeypatch.setattr(
        agent_route,
        "build_initial_agent_state",
        lambda question, ticker, mode: {
            "question": question,
            "ticker_hint": ticker or "",
            "mode": mode,
        },
    )
    monkeypatch.setattr(
        agent_route,
        "gather_discord_snapshot",
        lambda _state: {"resolved_ticker": "SPY", "discord_context": "digest"},
    )
    monkeypatch.setattr(
        agent_route,
        "build_smart_vs_retail",
        lambda _symbol: SimpleNamespace(retail_direction="bullish", retail_sentiment_score=72),
    )
    monkeypatch.setattr(
        agent_route,
        "fetch_market_bundle",
        lambda _state: {"market_bundle": '{"k":"v"}'},
    )
    monkeypatch.setattr(
        agent_route,
        "synthesize_llm_answer",
        lambda _state: {"answer": "测试回答"},
    )

    client = _build_client()
    resp = client.post(
        "/api/agent/query",
        json={"question": "分析SPY", "ticker": "SPY", "mode": "analysis"},
    )
    assert resp.status_code == 200
    events = _parse_sse_payloads(resp.text)
    assert len(events) >= 5
    # All non-terminal events should include ts_unix_ms
    for ev in events:
        if ev.get("type") in {"done", "answer"}:
            continue
        assert isinstance(ev.get("ts_unix_ms"), int)
        assert int(ev["ts_unix_ms"]) > 0
    assert any(ev.get("type") == "answer" and ev.get("content") == "测试回答" for ev in events)
