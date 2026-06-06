"""Commercial entitlement resolution across billing providers and MVP access keys."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import ApiEntitlementRow, Base
from app.db.models_user import UserRow
from app.services.access_keys import create_access_key
from app.services.entitlements import (
    CommercialTier,
    resolve_commercial_entitlement,
)
from app.services.passwords import hash_password


@pytest.fixture()
def db_session() -> Session:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(bind=engine)
    with TestingSessionLocal() as session:
        yield session


def _user(session: Session, *, role: str = "user", verified: bool = True) -> UserRow:
    row = UserRow(
        email=f"{role}-{verified}@example.com",
        password_hash=hash_password("Passw0rd1"),
        role=role,
        email_verified=verified,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def test_admin_jwt_wins_over_expired_provider(db_session: Session) -> None:
    user = _user(db_session, role="admin", verified=True)
    db_session.add(
        ApiEntitlementRow(
            api_key="legacy-admin",
            user_id=user.id,
            provider="creem",
            provider_status="canceled",
            plan="free",
        )
    )
    db_session.commit()

    entitlement = resolve_commercial_entitlement(db_session, user=user)

    assert entitlement.tier == CommercialTier.ADMIN
    assert entitlement.source == "admin"


def test_creem_active_subscription_grants_pro(db_session: Session) -> None:
    user = _user(db_session)
    db_session.add(
        ApiEntitlementRow(
            api_key="creem-sub",
            user_id=user.id,
            provider="creem",
            provider_customer_id="cus_123",
            provider_subscription_id="sub_123",
            provider_status="active",
            provider_price_id="prod_pro",
            plan="pro",
            current_period_end=datetime.now(timezone.utc) + timedelta(days=15),
        )
    )
    db_session.commit()

    entitlement = resolve_commercial_entitlement(db_session, user=user)

    assert entitlement.tier == CommercialTier.PRO
    assert entitlement.source == "creem"
    assert entitlement.provider_status == "active"


def test_past_due_provider_has_grace_period(db_session: Session) -> None:
    user = _user(db_session)
    db_session.add(
        ApiEntitlementRow(
            api_key="past-due-sub",
            user_id=user.id,
            provider="stripe",
            provider_status="past_due",
            past_due_since=datetime.now(timezone.utc) - timedelta(days=2),
            plan="pro",
        )
    )
    db_session.commit()

    entitlement = resolve_commercial_entitlement(db_session, user=user)

    assert entitlement.tier == CommercialTier.PRO
    assert entitlement.source == "stripe"
    assert entitlement.in_grace_period is True


def test_access_key_is_internal_pro_when_no_provider(db_session: Session) -> None:
    issued = create_access_key(db_session, key_type="trial", duration_days=7)

    entitlement = resolve_commercial_entitlement(
        db_session,
        raw_access_key=issued.raw_key,
        device_id="device-1",
    )

    assert entitlement.tier == CommercialTier.PRO
    assert entitlement.source == "access_key"


def test_verified_user_gets_trial_without_provider_or_access_key(db_session: Session) -> None:
    user = _user(db_session, verified=True)

    entitlement = resolve_commercial_entitlement(db_session, user=user, trial_enabled=True)

    assert entitlement.tier == CommercialTier.TRIAL
    assert entitlement.source == "jwt"


def test_guest_without_identity_or_key(db_session: Session) -> None:
    entitlement = resolve_commercial_entitlement(db_session)

    assert entitlement.tier == CommercialTier.GUEST
    assert entitlement.source == "anonymous"
