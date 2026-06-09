"""Integration tests for register/verify/login auth flow."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Generator

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes import auth as auth_route
from app.api.routes.auth import router as auth_router
from app.db.models import Base
from app.db.models_user import UserRow
from app.db.session import db_session_dep
from app.services.access_keys import create_access_key, validate_access_key
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
    monkeypatch.setattr(auth_route, "_deliver_verification_email", lambda **_kwargs: None)
    monkeypatch.setattr(
        auth_route,
        "get_settings",
        lambda: SimpleNamespace(
            auth_admin_emails="",
            auth_verification_code_ttl_seconds=900,
            auth_verification_max_attempts=5,
            auth_verification_debug_expose_code=True,
            turnstile_enabled=False,
            turnstile_secret_key="",
        ),
    )


def _apply_turnstile_required_settings(monkeypatch) -> None:
    monkeypatch.setattr(
        auth_route,
        "get_settings",
        lambda: SimpleNamespace(
            auth_admin_emails="",
            auth_verification_code_ttl_seconds=900,
            auth_verification_max_attempts=5,
            auth_verification_debug_expose_code=True,
            turnstile_enabled=True,
            turnstile_secret_key="test-secret",
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


def test_register_requires_turnstile_token_when_enabled(monkeypatch) -> None:
    _apply_auth_test_patches(monkeypatch)
    _apply_turnstile_required_settings(monkeypatch)
    client = _build_client()

    register_resp = client.post(
        "/api/auth/register",
        json={"email": "captcha-register@example.com", "password": "Passw0rd1"},
    )

    assert register_resp.status_code == 400
    assert register_resp.json()["detail"]["code"] == "turnstile_required"


def test_login_requires_turnstile_token_when_enabled(monkeypatch) -> None:
    _apply_auth_test_patches(monkeypatch)
    _apply_turnstile_required_settings(monkeypatch)
    client = _build_client()

    with client as test_client:
        app = test_client.app
        override_db = app.dependency_overrides[db_session_dep]
        session_gen = override_db()
        session = next(session_gen)
        try:
            row = UserRow(
                email="captcha-login@example.com",
                password_hash=hash_password("Passw0rd1"),
                role="user",
                email_verified=True,
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
        json={"email": "captcha-login@example.com", "password": "Passw0rd1"},
    )

    assert login_resp.status_code == 400
    assert login_resp.json()["detail"]["code"] == "turnstile_required"


def test_register_accepts_valid_turnstile_token_when_enabled(monkeypatch) -> None:
    _apply_auth_test_patches(monkeypatch)
    _apply_turnstile_required_settings(monkeypatch)
    posted: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"success": True, "action": "register"}

    class FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def post(self, url: str, data: dict[str, str]) -> FakeResponse:
            posted["url"] = url
            posted["data"] = data
            return FakeResponse()

    monkeypatch.setattr(auth_route.httpx, "Client", FakeClient)
    client = _build_client()

    register_resp = client.post(
        "/api/auth/register",
        json={
            "email": "captcha-ok@example.com",
            "password": "Passw0rd1",
            "turnstile_token": "valid-token",
        },
    )

    assert register_resp.status_code == 200
    assert register_resp.json()["user"]["email"] == "captcha-ok@example.com"
    assert posted["data"] == {
        "secret": "test-secret",
        "response": "valid-token",
        "remoteip": "testclient",
    }


def test_login_rejects_failed_turnstile_token_when_enabled(monkeypatch) -> None:
    _apply_auth_test_patches(monkeypatch)
    _apply_turnstile_required_settings(monkeypatch)

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"success": False, "error-codes": ["invalid-input-response"]}

    class FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def post(self, url: str, data: dict[str, str]) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr(auth_route.httpx, "Client", FakeClient)
    client = _build_client()

    login_resp = client.post(
        "/api/auth/login",
        json={
            "email": "captcha-fail@example.com",
            "password": "Passw0rd1",
            "turnstile_token": "bad-token",
        },
    )

    assert login_resp.status_code == 403
    assert login_resp.json()["detail"]["code"] == "turnstile_failed"


def test_resend_verification_requires_turnstile_token_when_enabled(monkeypatch) -> None:
    _apply_auth_test_patches(monkeypatch)
    client = _build_client()

    register_resp = client.post(
        "/api/auth/register",
        json={"email": "resend-captcha@example.com", "password": "Passw0rd1"},
    )
    assert register_resp.status_code == 200

    _apply_turnstile_required_settings(monkeypatch)
    resend_resp = client.post(
        "/api/auth/register/resend",
        json={"email": "resend-captcha@example.com"},
    )

    assert resend_resp.status_code == 400
    assert resend_resp.json()["detail"]["code"] == "turnstile_required"


def test_resend_verification_accepts_valid_turnstile_token_when_enabled(monkeypatch) -> None:
    _apply_auth_test_patches(monkeypatch)
    client = _build_client()

    register_resp = client.post(
        "/api/auth/register",
        json={"email": "resend-captcha-ok@example.com", "password": "Passw0rd1"},
    )
    assert register_resp.status_code == 200
    first_code = str(register_resp.json()["verification_code"])

    _apply_turnstile_required_settings(monkeypatch)
    posted: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"success": True, "action": "resend"}

    class FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def post(self, url: str, data: dict[str, str]) -> FakeResponse:
            posted["data"] = data
            return FakeResponse()

    monkeypatch.setattr(auth_route.httpx, "Client", FakeClient)
    resend_resp = client.post(
        "/api/auth/register/resend",
        json={"email": "resend-captcha-ok@example.com", "turnstile_token": "valid-token"},
    )

    assert resend_resp.status_code == 200
    second_code = str(resend_resp.json()["verification_code"])
    assert len(second_code) == 6
    assert second_code != first_code
    assert posted["data"] == {
        "secret": "test-secret",
        "response": "valid-token",
        "remoteip": "testclient",
    }


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


def test_admin_users_include_access_key_summary(monkeypatch) -> None:
    _apply_auth_test_patches(monkeypatch)
    client = _build_client()

    with client as test_client:
        app = test_client.app
        override_db = app.dependency_overrides[db_session_dep]
        session_gen = override_db()
        session = next(session_gen)
        try:
            admin = UserRow(
                email="admin@example.com",
                password_hash=hash_password("Passw0rd1"),
                role="admin",
                email_verified=True,
            )
            user = UserRow(
                email="paid@example.com",
                password_hash=hash_password("Passw0rd1"),
                role="user",
                email_verified=True,
            )
            session.add_all([admin, user])
            session.commit()
            issued = create_access_key(session, key_type="paid", duration_days=30, note="paid user")
            validate_access_key(
                session,
                raw_key=issued.raw_key,
                device_id="device-a",
                user=user,
                email=user.email,
            )
        finally:
            session.close()
            try:
                next(session_gen)
            except StopIteration:
                pass

    login_resp = client.post(
        "/api/auth/login",
        json={"email": "admin@example.com", "password": "Passw0rd1"},
    )
    assert login_resp.status_code == 200
    token = login_resp.json()["access_token"]

    users_resp = client.get("/api/auth/admin/users", headers={"Authorization": f"Bearer {token}"})
    assert users_resp.status_code == 200
    paid = next(row for row in users_resp.json() if row["email"] == "paid@example.com")
    assert paid["access_keys"]["total"] == 1
    assert paid["access_keys"]["active"] == 1
    assert paid["access_keys"]["latest_key_prefix"].startswith("aji_paid_")
    assert paid["access_keys"]["latest_days_remaining"] >= 29


def test_register_fails_when_email_send_fails(monkeypatch) -> None:
    _apply_auth_test_patches(monkeypatch)

    def _fail_send(**_kwargs: object) -> None:
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "email_send_failed", "message": "验证码邮件发送失败，请稍后重试。"},
        )

    monkeypatch.setattr(auth_route, "_deliver_verification_email", _fail_send)
    client = _build_client()

    register_resp = client.post(
        "/api/auth/register",
        json={"email": "fail-send@example.com", "password": "Passw0rd1"},
    )
    assert register_resp.status_code == 503
    assert register_resp.json()["detail"]["code"] == "email_send_failed"

    with client as test_client:
        app = test_client.app
        override_db = app.dependency_overrides[db_session_dep]
        session_gen = override_db()
        session = next(session_gen)
        try:
            row = session.execute(
                select(UserRow).where(UserRow.email == "fail-send@example.com")
            ).scalar_one_or_none()
            assert row is None
        finally:
            session.close()
            try:
                next(session_gen)
            except StopIteration:
                pass


def test_resend_verification_for_unverified_user(monkeypatch) -> None:
    _apply_auth_test_patches(monkeypatch)
    client = _build_client()

    register_resp = client.post(
        "/api/auth/register",
        json={"email": "resend@example.com", "password": "Passw0rd1"},
    )
    assert register_resp.status_code == 200
    first_code = str(register_resp.json()["verification_code"])

    resend_resp = client.post(
        "/api/auth/register/resend",
        json={"email": "resend@example.com"},
    )
    assert resend_resp.status_code == 200
    second_code = str(resend_resp.json()["verification_code"])
    assert len(second_code) == 6
    assert second_code != first_code

    verify_resp = client.post(
        "/api/auth/register/verify",
        json={"email": "resend@example.com", "code": second_code},
    )
    assert verify_resp.status_code == 200
    assert verify_resp.json()["user"]["email_verified"] is True


def test_unverified_reregister_with_same_password_resends_code(monkeypatch) -> None:
    _apply_auth_test_patches(monkeypatch)
    client = _build_client()

    first = client.post(
        "/api/auth/register",
        json={"email": "reregister@example.com", "password": "Passw0rd1"},
    )
    assert first.status_code == 200
    first_code = str(first.json()["verification_code"])

    second = client.post(
        "/api/auth/register",
        json={"email": "reregister@example.com", "password": "Passw0rd1"},
    )
    assert second.status_code == 200
    second_code = str(second.json()["verification_code"])
    assert len(second_code) == 6
    assert second_code != first_code
