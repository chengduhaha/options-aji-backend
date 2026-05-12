"""Agent / paid API access: legacy tokens, Stripe entitlements, free-tier quotas."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import ApiEntitlementRow, UsageDailyRow
from app.db.session import db_session_dep

logger = logging.getLogger(__name__)


def _utc_day_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


def _legacy_tokens(settings: Settings) -> set[str]:
    return {t.strip() for t in settings.subscription_tokens.split(",") if t.strip()}


def extract_bearer_token(authorization: Optional[str]) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        return ""
    return authorization.removeprefix("Bearer ").strip()


def _pro_period_active(row: ApiEntitlementRow) -> bool:
    if row.plan != "pro":
        return False
    if row.current_period_end is None:
        return True
    end = row.current_period_end
    if end.tzinfo is None:
        end = end.replace(tzinfo=dt.timezone.utc)
    return end > dt.datetime.now(dt.timezone.utc)


def _usage_today(session: Session, api_key: str, day: str) -> int:
    row = session.execute(
        select(UsageDailyRow).where(
            UsageDailyRow.api_key == api_key,
            UsageDailyRow.usage_date == day,
        ),
    ).scalar_one_or_none()
    return int(row.agent_queries) if row is not None else 0


def usage_agent_queries_today(session: Session, api_key: str) -> int:
    return _usage_today(session, api_key, _utc_day_iso())


def _increment_agent_usage(session: Session, api_key: str, day: str) -> None:
    row = session.execute(
        select(UsageDailyRow).where(
            UsageDailyRow.api_key == api_key,
            UsageDailyRow.usage_date == day,
        ),
    ).scalar_one_or_none()
    if row is None:
        session.add(
            UsageDailyRow(api_key=api_key, usage_date=day, agent_queries=1),
        )
    else:
        row.agent_queries = int(row.agent_queries) + 1
    session.commit()


def ensure_agent_billing(
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    session: Session = Depends(db_session_dep),
) -> str:
    """Require Bearer token when Stripe or strict subscription mode is on; enforce quotas for free tier."""

    settings = get_settings()
    token = extract_bearer_token(authorization)
    stripe_on = bool(settings.stripe_secret_key.strip())
    need_auth = settings.subscription_required or stripe_on

    if not need_auth:
        return token

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "unauthorized", "message": "需要 Authorization: Bearer API 密钥。"},
        )

    if token in _legacy_tokens(settings):
        return token

    if not stripe_on:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_token", "message": "令牌无效。"},
        )

    row = session.get(ApiEntitlementRow, token)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "unknown_api_key",
                "message": "未知 API 密钥：请在设置页使用同一密钥完成 Stripe 结账以绑定账户。",
            },
        )

    if row.plan == "pro" and _pro_period_active(row):
        return token

    limit = max(0, int(settings.free_tier_daily_agent_queries))
    day = _utc_day_iso()
    used = _usage_today(session, token, day)
    if used >= limit:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "code": "quota_exceeded",
                "message": (
                    f"今日 AI 请求已达 Free 上限（{limit} 次）。请升级 Pro 或明日再试。"
                ),
            },
        )

    _increment_agent_usage(session, token, day)
    logger.debug("Agent usage increment key=%s day=%s -> %s", token[:6], day, used + 1)
    return token
