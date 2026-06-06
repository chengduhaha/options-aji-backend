"""Agent billing access through unified commercial entitlements."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import billing_access
from app.db.models import ApiEntitlementRow, Base
from app.db.models_user import UserRow
from app.services.jwt_tokens import create_access_token
from app.services.passwords import hash_password


def _session() -> Session:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)()


def test_agent_billing_accepts_creem_pro_jwt(monkeypatch) -> None:
    session = _session()
    try:
        user = UserRow(
            email="creem-pro@example.com",
            password_hash=hash_password("Passw0rd1"),
            role="user",
            email_verified=True,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        session.add(
            ApiEntitlementRow(
                api_key=f"creem:{user.id}",
                user_id=user.id,
                provider="creem",
                provider_status="active",
                plan="pro",
                current_period_end=datetime.now(timezone.utc) + timedelta(days=10),
            )
        )
        session.commit()
        token = create_access_token(user_id=user.id, email=user.email, role=user.role)
        monkeypatch.setattr(
            billing_access,
            "get_settings",
            lambda: SimpleNamespace(
                creem_api_key="creem_test",
                creem_product_id_pro="prod_pro",
                stripe_secret_key="",
                subscription_required=False,
                subscription_tokens="",
                free_tier_daily_agent_queries=20,
                mvp_trial_enabled=True,
            ),
        )

        result = billing_access.ensure_agent_billing(
            authorization=f"Bearer {token}",
            session=session,
        )

        assert result == f"user:{user.id}"
    finally:
        session.close()
