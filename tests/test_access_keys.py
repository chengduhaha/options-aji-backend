from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Generator

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps_access_key import AccessKeyGrant, require_access_key
from app.db.models import AccessKeyRow, Base
from app.db.session import db_session_dep
from app.services.access_keys import (
    create_access_key,
    delete_access_key,
    inspect_access_key,
    patch_access_key,
    unbind_access_key_device,
    validate_access_key,
)


def _build_session() -> tuple[sessionmaker[Session], FastAPI]:
    app = FastAPI()
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
    return TestingSessionLocal, app


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def test_trial_key_activates_on_first_valid_use_and_binds_device() -> None:
    TestingSessionLocal, _ = _build_session()
    with TestingSessionLocal() as session:
        issued = create_access_key(session, key_type="trial", duration_days=7, note="free trial")

        grant = validate_access_key(
            session,
            raw_key=issued.raw_key,
            device_id="device-a",
            email="trial@example.com",
        )

        row = session.execute(select(AccessKeyRow)).scalar_one()
        assert grant.key_prefix == issued.key_prefix
        assert row.key_type == "trial"
        assert row.bound_device_id == "device-a"
        assert row.bound_email == "trial@example.com"
        assert row.activated_at is not None
        assert row.expires_at is not None
        assert timedelta(days=6, hours=23) < _utc(row.expires_at) - _utc(row.activated_at) <= timedelta(days=7)
        assert row.usage_count == 1


def test_access_key_rejects_different_device_after_binding() -> None:
    TestingSessionLocal, _ = _build_session()
    with TestingSessionLocal() as session:
        issued = create_access_key(session, key_type="trial", duration_days=7)
        validate_access_key(session, raw_key=issued.raw_key, device_id="device-a")

        try:
            validate_access_key(session, raw_key=issued.raw_key, device_id="device-b")
        except Exception as exc:
            assert getattr(exc, "status_code", None) == 403
            assert getattr(exc, "detail", {}).get("code") == "device_mismatch"
        else:
            raise AssertionError("different device should be rejected")


def test_access_key_rejects_expired_key() -> None:
    TestingSessionLocal, _ = _build_session()
    with TestingSessionLocal() as session:
        issued = create_access_key(session, key_type="paid", duration_days=30)
        validate_access_key(session, raw_key=issued.raw_key, device_id="device-a")
        row = session.execute(select(AccessKeyRow)).scalar_one()
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.commit()

        try:
            validate_access_key(session, raw_key=issued.raw_key, device_id="device-a")
        except Exception as exc:
            assert getattr(exc, "status_code", None) == 402
            assert getattr(exc, "detail", {}).get("code") == "access_key_expired"
        else:
            raise AssertionError("expired key should be rejected")


def test_require_access_key_dependency_accepts_valid_headers() -> None:
    TestingSessionLocal, app = _build_session()

    @app.get("/protected")
    def protected(_: AccessKeyGrant = Depends(require_access_key)) -> dict[str, bool]:
        return {"ok": True}

    with TestingSessionLocal() as session:
        issued = create_access_key(session, key_type="trial", duration_days=7)

    client = TestClient(app)
    missing = client.get("/protected")
    assert missing.status_code == 401

    ok = client.get(
        "/protected",
        headers={"X-Access-Key": issued.raw_key, "X-Device-Id": "browser-1"},
    )
    assert ok.status_code == 200
    assert ok.json() == {"ok": True}


def test_inspect_access_key_does_not_increment_usage() -> None:
    TestingSessionLocal, _ = _build_session()
    with TestingSessionLocal() as session:
        issued = create_access_key(session, key_type="trial", duration_days=7)
        status_row = inspect_access_key(
            session,
            raw_key=issued.raw_key,
            device_id="device-a",
            commit_usage=False,
        )
        row = session.execute(select(AccessKeyRow)).scalar_one()
        assert status_row.valid is True
        assert status_row.status == "pending"
        assert row.usage_count == 0
        assert row.activated_at is None
        assert row.bound_device_id is None


def test_inspect_after_validate_increments_usage_once() -> None:
    TestingSessionLocal, _ = _build_session()
    with TestingSessionLocal() as session:
        issued = create_access_key(session, key_type="trial", duration_days=7)
        inspect_access_key(session, raw_key=issued.raw_key, device_id="device-a", commit_usage=False)
        validate_access_key(session, raw_key=issued.raw_key, device_id="device-a")
        row = session.execute(select(AccessKeyRow)).scalar_one()
        assert row.usage_count == 1
        inspect_access_key(session, raw_key=issued.raw_key, device_id="device-a", commit_usage=False)
        row = session.execute(select(AccessKeyRow)).scalar_one()
        assert row.usage_count == 1


def test_unbind_device_allows_new_device() -> None:
    TestingSessionLocal, _ = _build_session()
    with TestingSessionLocal() as session:
        issued = create_access_key(session, key_type="trial", duration_days=7)
        validate_access_key(session, raw_key=issued.raw_key, device_id="device-a")
        unbind_access_key_device(session, key_prefix=issued.key_prefix)
        validate_access_key(session, raw_key=issued.raw_key, device_id="device-b")
        row = session.execute(select(AccessKeyRow)).scalar_one()
        assert row.bound_device_id == "device-b"


def test_delete_unactivated_key() -> None:
    TestingSessionLocal, _ = _build_session()
    with TestingSessionLocal() as session:
        issued = create_access_key(session, key_type="trial", duration_days=7)
        assert delete_access_key(session, key_prefix=issued.key_prefix) is True
        assert session.execute(select(AccessKeyRow)).scalar_one_or_none() is None


def test_patch_note_and_expires_at() -> None:
    TestingSessionLocal, _ = _build_session()
    with TestingSessionLocal() as session:
        issued = create_access_key(session, key_type="paid", duration_days=30)
        future = datetime.now(timezone.utc) + timedelta(days=60)
        row = patch_access_key(
            session,
            key_prefix=issued.key_prefix,
            note="vip user",
            expires_at=future,
        )
        assert row.note == "vip user"
        assert row.activated_at is not None
        assert _utc(row.expires_at) == future
