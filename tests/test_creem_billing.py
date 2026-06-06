"""Creem billing checkout, status, portal, and webhook behavior."""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from typing import Generator

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes.creem_billing import router as creem_router
from app.config import Settings, get_settings
from app.db.models import ApiEntitlementRow, Base, PaymentWebhookEventRow
from app.db.models_user import UserRow
from app.db.session import db_session_dep
from app.services.jwt_tokens import create_access_token
from app.services.passwords import hash_password


def _build_client(settings: Settings) -> tuple[TestClient, sessionmaker[Session]]:
    app = FastAPI()
    app.include_router(creem_router)
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
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app), TestingSessionLocal


def _create_user(session: Session) -> tuple[UserRow, str]:
    user = UserRow(
        email="creem@example.com",
        password_hash=hash_password("Passw0rd1"),
        role="user",
        email_verified=True,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    token = create_access_token(user_id=user.id, email=user.email, role=user.role)
    return user, token


def _signature(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def test_checkout_returns_503_when_creem_is_not_configured() -> None:
    client, TestingSessionLocal = _build_client(Settings(creem_api_key="", creem_product_id_pro=""))
    with TestingSessionLocal() as session:
        _, token = _create_user(session)

    resp = client.post("/api/creem/checkout", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "creem_not_configured"


def test_checkout_creates_dynamic_session_with_user_metadata(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"checkout_url": "https://checkout.creem.io/ch_dynamic"}

    def fake_post(url: str, **kwargs: object) -> FakeResponse:
        captured["url"] = url
        captured["kwargs"] = kwargs
        return FakeResponse()

    monkeypatch.setattr("app.api.routes.creem_billing.httpx.post", fake_post)
    settings = Settings(
        creem_api_key="creem_test",
        creem_product_id_pro="prod_pro",
        creem_success_url="https://optionsaji.com/profile?billing=success",
        creem_cancel_url="https://optionsaji.com/profile?billing=cancel",
    )
    client, TestingSessionLocal = _build_client(settings)
    with TestingSessionLocal() as session:
        user, token = _create_user(session)

    resp = client.post("/api/creem/checkout", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200
    assert resp.json()["url"] == "https://checkout.creem.io/ch_dynamic"
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    payload = kwargs["json"]
    assert isinstance(payload, dict)
    assert payload["request_id"] == user.id
    assert payload["metadata"]["user_id"] == user.id
    assert payload["metadata"]["email"] == user.email
    assert payload["customer"]["email"] == user.email


def test_webhook_upserts_active_subscription_and_is_idempotent() -> None:
    secret = "whsec_test"
    client, TestingSessionLocal = _build_client(Settings(creem_webhook_secret=secret))
    with TestingSessionLocal() as session:
        user, _ = _create_user(session)
        user_id = user.id

    body = (
        b'{"id":"evt_1","eventType":"subscription.active","object":'
        b'{"customer":{"id":"cus_1"},"subscription":{"id":"sub_1","status":"active",'
        b'"current_period_end":"2026-07-01T00:00:00Z"},"product":{"id":"prod_1"},'
        b'"metadata":{"userId":"'
        + user_id.encode("utf-8")
        + b'"}}}'
    )
    headers = {"creem-signature": _signature(body, secret)}

    first = client.post("/api/creem/webhook", content=body, headers=headers)
    second = client.post("/api/creem/webhook", content=body, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    with TestingSessionLocal() as session:
        events = session.scalars(select(PaymentWebhookEventRow)).all()
        row = session.scalars(
            select(ApiEntitlementRow).where(ApiEntitlementRow.user_id == user_id)
        ).one()
        assert len(events) == 1
        assert row.provider == "creem"
        assert row.provider_status == "active"
        assert row.plan == "pro"
        assert row.current_period_end is not None


def test_status_reports_current_jwt_user_subscription() -> None:
    client, TestingSessionLocal = _build_client(Settings())
    with TestingSessionLocal() as session:
        user, token = _create_user(session)
        session.add(
            ApiEntitlementRow(
                api_key=f"creem:{user.id}",
                user_id=user.id,
                provider="creem",
                provider_status="trialing",
                provider_customer_id="cus_1",
                plan="pro",
                current_period_end=datetime(2026, 7, 1, tzinfo=timezone.utc),
            )
        )
        session.commit()

    resp = client.get("/api/creem/status", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["tier"] == "pro"
    assert body["source"] == "creem"
    assert body["provider_status"] == "trialing"
