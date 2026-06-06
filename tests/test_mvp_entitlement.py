"""Tests for MVP guest/trial/pro tier resolution and redaction."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Generator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps_access_key import resolve_mvp_entitlement
from app.api.routes import mvp as mvp_route
from app.api.routes.mvp import router as mvp_router
from app.db.models import AccessKeyRow, Base
from app.db.models_user import UserRow
from app.db.session import db_session_dep
from app.services.access_keys import create_access_key
from app.services.jwt_tokens import create_access_token
from app.services.mvp_entitlement import (
    redact_market_insights,
    redact_stock_insights,
    redact_war_room,
)
from app.services.passwords import hash_password


def _build_mvp_client() -> tuple[TestClient, sessionmaker[Session]]:
    app = FastAPI()
    app.include_router(mvp_router)
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(bind=engine)

    def override_db() -> Generator[Session, None, None]:
        session = TestingSessionLocal()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[db_session_dep] = override_db
    return TestClient(app), TestingSessionLocal


def test_redact_war_room_guest_strips_sensitive_fields() -> None:
    payload = {
        "events": [
            {
                "title": "Fed risk",
                "deep_dive_zh": "secret",
                "trade_implications_zh": "trade",
                "body": "long body",
            },
            {"title": "Second"},
            {"title": "Third"},
        ],
        "trade_plan": ["plan a", "plan b"],
        "summary_zh": "x" * 200,
        "treasury_read": {"summary_zh": "y" * 100},
    }
    out = redact_war_room(payload, "guest")
    assert len(out["events"]) == 2
    assert "deep_dive_zh" not in out["events"][0]
    assert out["trade_plan"] == []
    assert len(out["summary_zh"]) <= 120


def test_redact_market_insights_guest_clears_reasoning() -> None:
    payload = {
        "regime": {
            "code": "risk_on",
            "label": "风险偏好",
            "summary": "s" * 200,
            "reasoning": "hidden reasoning",
            "basis": ["a", "b"],
        },
        "vix": {"interpretation": "full vix text"},
    }
    out = redact_market_insights(payload, "guest")
    assert out["regime"]["reasoning"] == ""
    assert out["regime"]["basis"] == []
    assert out["vix"]["interpretation"] == "登录后查看完整解读"


def test_redact_stock_insights_trial_truncates() -> None:
    payload = {
        "framework_summary": "ok",
        "contracts_note": "full contracts",
        "combined_insight": "z" * 400,
        "expected_moves": [{"bucket": "1w"}, {"bucket": "2w"}, {"bucket": "1m"}],
        "engine": "rules",
        "generated_at_utc": "2026-01-01T00:00:00Z",
    }
    out = redact_stock_insights(payload, "trial")
    assert "Pro" in out["contracts_note"]
    assert len(out["expected_moves"]) == 2


def test_war_room_guest_returns_tier_without_access_key(monkeypatch) -> None:
    monkeypatch.setattr(mvp_route, "list_discord_feed_rows", lambda *_a, **_k: [])
    monkeypatch.setattr(
        mvp_route,
        "_get_cached_war_room_llm",
        lambda *_a, **_k: None,
    )
    client, _ = _build_mvp_client()
    resp = client.get("/api/mvp/war-room?hours=6")
    assert resp.status_code == 200
    body = resp.json()
    assert body["tier"] == "guest"
    assert "events" in body


def test_stock_options_guest_requires_login() -> None:
    client, _ = _build_mvp_client()
    resp = client.post(
        "/api/mvp/stock-options-insights",
        json={"symbol": "SPY", "direction": "bull"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "login_required"


@pytest.mark.asyncio
async def test_resolve_mvp_entitlement_trial_for_verified_user() -> None:
    _, TestingSessionLocal = _build_mvp_client()
    with TestingSessionLocal() as session:
        user = UserRow(
            email="trial@example.com",
            password_hash=hash_password("Passw0rd1"),
            role="user",
            email_verified=True,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        token = create_access_token(user_id=user.id, email=user.email, role=user.role)

    entitlement = await resolve_mvp_entitlement(
        x_access_key=None,
        x_device_id=None,
        x_user_email=None,
        authorization=f"Bearer {token}",
        session=session,
    )
    assert entitlement.tier == "trial"


@pytest.mark.asyncio
async def test_resolve_mvp_entitlement_pro_with_access_key() -> None:
    _, TestingSessionLocal = _build_mvp_client()
    with TestingSessionLocal() as session:
        issued = create_access_key(session, key_type="paid", duration_days=30)
        entitlement = await resolve_mvp_entitlement(
            x_access_key=issued.raw_key,
            x_device_id="device-1",
            x_user_email=None,
            authorization=None,
            session=session,
        )
    assert entitlement.tier == "pro"
    assert entitlement.grant is not None


def test_war_room_pro_with_access_key_full_events(monkeypatch) -> None:
    monkeypatch.setattr(mvp_route, "list_discord_feed_rows", lambda *_a, **_k: [])
    monkeypatch.setattr(mvp_route, "_get_cached_war_room_llm", lambda *_a, **_k: None)
    client, TestingSessionLocal = _build_mvp_client()
    with TestingSessionLocal() as session:
        issued = create_access_key(session, key_type="paid", duration_days=30)
        raw_key = issued.raw_key
    resp = client.get(
        "/api/mvp/war-room?hours=6",
        headers={"X-Access-Key": raw_key, "X-Device-Id": "device-war"},
    )
    assert resp.status_code == 200
    assert resp.json()["tier"] == "pro"
