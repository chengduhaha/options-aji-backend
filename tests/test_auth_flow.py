"""Integration tests for register/verify/login auth flow."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Generator

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes import auth as auth_route
from app.api.routes.auth import router as auth_router
from app.db.models import Base
from app.db.models_user import UserRow
from app.db.session import db_session_dep
from app.services.passwords import hash_password


def _build_client() -> TestClient:
    app = FastAPI()
    app.include_router(auth_router)

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
    return TestClient(app)


def _apply_auth_test_patches(monkeypatch) -> None:
    monkeypatch.setattr(auth_route, "register_rate_limited", lambda _ip: False)
    monkeypatch.setattr(auth_route, "is_login_locked", lambda _email: False)
    monkeypatch.setattr(auth_route, "record_login_failure", lambda _email: None)
    monkeypatch.setattr(auth_route, "clear_login_failure", lambda _email: None)
    monkeypatch.setattr(
        auth_route,
        "get_settings",
        lambda: SimpleNamespace(
            auth_admin_emails="",
            auth_verification_code_ttl_seconds=900,
            auth_verification_max_attempts=5,
            auth_verification_debug_expose_code=True,
        ),
    )


def test_register_verify_login_roundtrip(monkeypatch) -> None:
    _apply_auth_test_patches(monkeypatch)
    client = _build_client()

    register_resp = client.post(
        "/api/auth/register",
        json={"email": "auth-flow@example.com", "password": "Passw0rd1", "display_name": "Flow"},
    )
    assert register_resp.status_code == 200
    register_json = register_resp.json()
    assert register_json["verification_required"] is True
    assert register_json["user"]["email_verified"] is False
    verification_code = str(register_json["verification_code"])
    assert len(verification_code) == 6

    blocked_login_resp = client.post(
        "/api/auth/login",
        json={"email": "auth-flow@example.com", "password": "Passw0rd1"},
    )
    assert blocked_login_resp.status_code == 403
    assert blocked_login_resp.json()["detail"]["code"] == "email_not_verified"

    wrong_verify_resp = client.post(
        "/api/auth/register/verify",
        json={"email": "auth-flow@example.com", "code": "111111"},
    )
    assert wrong_verify_resp.status_code == 401
    assert wrong_verify_resp.json()["detail"]["code"] == "invalid_code"

    verify_resp = client.post(
        "/api/auth/register/verify",
        json={"email": "auth-flow@example.com", "code": verification_code},
    )
    assert verify_resp.status_code == 200
    verify_json = verify_resp.json()
    assert verify_json["access_token"]
    assert verify_json["user"]["email_verified"] is True

    login_resp = client.post(
        "/api/auth/login",
        json={"email": "auth-flow@example.com", "password": "Passw0rd1"},
    )
    assert login_resp.status_code == 200
    login_json = login_resp.json()
    assert login_json["access_token"]
    assert login_json["user"]["email"] == "auth-flow@example.com"


def test_legacy_unverified_user_can_still_login_without_pending_verification(monkeypatch) -> None:
    _apply_auth_test_patches(monkeypatch)
    client = _build_client()

    with client as test_client:
        app = test_client.app
        override_db = app.dependency_overrides[db_session_dep]
        session_gen = override_db()
        session = next(session_gen)
        try:
            row = UserRow(
                email="legacy@example.com",
                password_hash=hash_password("Passw0rd1"),
                role="user",
                email_verified=False,
            )
            session.add(row)
            session.commit()
        finally:
            session.close()
            try:
                next(session_gen)
            except StopIteration:
                pass

    login_resp = client.post(
        "/api/auth/login",
        json={"email": "legacy@example.com", "password": "Passw0rd1"},
    )
    assert login_resp.status_code == 200
    assert login_resp.json()["user"]["email"] == "legacy@example.com"
