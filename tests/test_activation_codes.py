"""Tests for activation code membership system."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Generator

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes import auth as auth_route
from app.api.routes.activation_codes import router as activation_codes_router
from app.api.routes.auth import router as auth_router
from app.api.routes.options import router as options_router
from app.db.models import Base
from app.db.models_user import ActivationCodeRow, UserRow
from app.db.session import db_session_dep
from app.services.activation_codes import generate_activation_codes
from app.services.membership import resolve_v3_access
from app.services.passwords import hash_password
from app.services.v3_board_access import apply_leaderboard_access, enforce_gex_symbol_access


def _build_client(*routers) -> tuple[TestClient, sessionmaker]:
    app = FastAPI()
    for router in routers:
        app.include_router(router)

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


def _apply_auth_patches(monkeypatch) -> None:
    monkeypatch.setattr(auth_route, "register_rate_limited", lambda _ip: False)
    monkeypatch.setattr(auth_route, "is_login_locked", lambda _email: False)
    monkeypatch.setattr(auth_route, "record_login_failure", lambda _email: None)
    monkeypatch.setattr(auth_route, "clear_login_failure", lambda _email: None)
    monkeypatch.setattr(auth_route, "_deliver_verification_email", lambda **_kwargs: None)
    monkeypatch.setattr(
        auth_route,
        "get_settings",
        lambda: SimpleNamespace(
            auth_admin_emails="admin@test.com",
            auth_verification_code_ttl_seconds=900,
            auth_verification_max_attempts=5,
            auth_verification_debug_expose_code=True,
            turnstile_enabled=False,
            turnstile_secret_key="",
        ),
    )


def _register_and_verify(client: TestClient, email: str, password: str = "Secret123") -> str:
    reg = client.post(
        "/api/auth/register",
        json={"email": email, "password": password},
    )
    assert reg.status_code == 200, reg.text
    code = reg.json()["verification_code"]
    verify = client.post(
        "/api/auth/register/verify",
        json={"email": email, "code": code},
    )
    assert verify.status_code == 200, verify.text
    return verify.json()["access_token"]


def test_generate_and_redeem_code(monkeypatch) -> None:
    _apply_auth_patches(monkeypatch)
    client, SessionLocal = _build_client(auth_router, activation_codes_router)

    with SessionLocal() as session:
        admin = UserRow(
            email="admin@test.com",
            password_hash=hash_password("Admin1234"),
            role="admin",
            email_verified=True,
        )
        session.add(admin)
        session.commit()
        session.refresh(admin)
        batch_id, codes = generate_activation_codes(
            session,
            duration_tier="7D",
            count=1,
            admin=admin,
        )
        assert batch_id
        assert len(codes) == 1
        assert codes[0].startswith("OAJI-7D-")

    user_token = _register_and_verify(client, "member@test.com")
    redeem = client.post(
        "/api/auth/redeem",
        json={"code": codes[0]},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert redeem.status_code == 200, redeem.text
    body = redeem.json()
    assert body["user"]["membership"]["is_member"] is True
    assert body["membership_expires_at"]

    dup = client.post(
        "/api/auth/redeem",
        json={"code": codes[0]},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert dup.status_code == 409


def test_stack_renewal_extends_from_current_expiry(monkeypatch) -> None:
    _apply_auth_patches(monkeypatch)
    _, SessionLocal = _build_client(auth_router, activation_codes_router)

    with SessionLocal() as session:
        admin = UserRow(
            email="admin@test.com",
            password_hash=hash_password("Admin1234"),
            role="admin",
            email_verified=True,
        )
        user = UserRow(
            email="stack@test.com",
            password_hash=hash_password("Secret123"),
            role="user",
            email_verified=True,
            membership_expires_at=datetime.now(timezone.utc) + timedelta(days=10),
        )
        session.add_all([admin, user])
        session.commit()
        session.refresh(admin)

        _, codes = generate_activation_codes(
            session,
            duration_tier="30D",
            count=1,
            admin=admin,
        )
        from app.services.activation_codes import redeem_activation_code
        from app.services.membership import _as_aware

        updated = redeem_activation_code(
            session,
            user=user,
            raw_code=codes[0],
            client_ip="127.0.0.1",
        )
        assert updated.membership_expires_at is not None
        expires = _as_aware(updated.membership_expires_at)
        assert expires is not None
        remaining = expires - datetime.now(timezone.utc)
        assert remaining.days >= 39


def test_leaderboard_free_user_gets_ten_rows() -> None:
    payload = {
        "board": "volume",
        "items": [{"rank": i, "symbol": f"S{i}"} for i in range(1, 51)],
        "total": 50,
    }
    access = resolve_v3_access(None)
    gated = apply_leaderboard_access(payload, board_id="volume", access=access)
    assert len(gated["items"]) == 10
    assert gated["access"]["row_limit"] == 10


def test_leaderboard_previously_locked_board_shows_ten_rows() -> None:
    payload = {
        "board": "seller",
        "items": [{"rank": i, "underlying": f"SYM{i}"} for i in range(1, 21)],
        "total": 20,
    }
    access = resolve_v3_access(None)
    gated = apply_leaderboard_access(payload, board_id="seller", access=access)
    assert gated["locked"] is False
    assert len(gated["items"]) == 10
    assert gated["access"]["row_limit"] == 10


def test_leaderboard_symbol_masking_ranks_one_to_three() -> None:
    payload = {
        "board": "high-iv",
        "items": [{"rank": i, "underlying": f"SYM{i}"} for i in range(1, 11)],
        "total": 10,
    }
    access = resolve_v3_access(None)
    gated = apply_leaderboard_access(payload, board_id="high-iv", access=access)
    assert len(gated["items"]) == 10
    assert gated["items"][0]["symbol_masked"] is True
    assert gated["items"][0]["underlying"] == ""
    assert gated["items"][2]["symbol_masked"] is True
    assert gated["items"][3]["symbol_masked"] is False
    assert gated["items"][3]["underlying"] == "SYM4"
    assert gated["items"][9]["symbol_masked"] is False
    assert gated["items"][9]["underlying"] == "SYM10"


def test_leaderboard_member_gets_unmasked_rows() -> None:
    payload = {
        "board": "seller",
        "items": [{"rank": i, "underlying": f"SYM{i}"} for i in range(1, 6)],
        "total": 5,
    }
    user = SimpleNamespace(
        role="member",
        membership_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    access = resolve_v3_access(user)
    gated = apply_leaderboard_access(payload, board_id="seller", access=access)
    assert gated["locked"] is False
    assert len(gated["items"]) == 5
    assert gated["items"][0].get("symbol_masked") is not True
    assert gated["items"][0]["underlying"] == "SYM1"


def test_gex_non_spy_blocked_for_free_user() -> None:
    access = resolve_v3_access(None)
    try:
        enforce_gex_symbol_access("QQQ", access)
        assert False, "expected HTTPException"
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 403


def test_admin_generate_requires_admin_role(monkeypatch) -> None:
    _apply_auth_patches(monkeypatch)
    client, SessionLocal = _build_client(auth_router, activation_codes_router)

    with SessionLocal() as session:
        admin = UserRow(
            email="admin@test.com",
            password_hash=hash_password("Admin1234"),
            role="admin",
            email_verified=True,
        )
        session.add(admin)
        session.commit()

    user_token = _register_and_verify(client, "user@test.com")
    denied = client.post(
        "/api/admin/activation-codes/generate",
        json={"duration_tier": "7D", "count": 1},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert denied.status_code == 403

    admin_login = client.post(
        "/api/auth/login",
        json={"email": "admin@test.com", "password": "Admin1234"},
    )
    assert admin_login.status_code == 200, admin_login.text
    admin_token = admin_login.json()["access_token"]
    ok = client.post(
        "/api/admin/activation-codes/generate",
        json={"duration_tier": "365D", "count": 2},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["count"] == 2
    assert all(code.startswith("OAJI-365D-") for code in body["codes"])
