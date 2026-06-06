"""Creem Checkout, portal, status, and webhooks."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps_auth import get_current_user
from app.config import Settings, get_settings
from app.db.models import ApiEntitlementRow, PaymentWebhookEventRow
from app.db.models_user import UserRow
from app.db.session import db_session_dep
from app.services.entitlements import CommercialTier, resolve_commercial_entitlement

logger = logging.getLogger(__name__)

router = APIRouter(tags=["creem"])


class CheckoutResponse(BaseModel):
    url: str


class PortalResponse(BaseModel):
    url: str


def _aware_utc(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


def _parse_period_end(value: Any) -> dt.datetime | None:
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(float(value), tz=dt.timezone.utc)
    if isinstance(value, str) and value.strip():
        raw = value.strip()
        if raw.isdigit():
            return dt.datetime.fromtimestamp(float(raw), tz=dt.timezone.utc)
        try:
            return dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _dig(data: dict[str, Any], *keys: str) -> Any:
    cur: Any = data
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _event_type(payload: dict[str, Any]) -> str:
    for key in ("eventType", "event_type", "type"):
        raw = payload.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return ""


def _event_object(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("object", "data"):
        raw = payload.get(key)
        if isinstance(raw, dict):
            nested = raw.get("object")
            return nested if isinstance(nested, dict) else raw
    return payload


def _subscription_payload(obj: dict[str, Any]) -> dict[str, Any]:
    raw = obj.get("subscription")
    return raw if isinstance(raw, dict) else obj


def _metadata_payload(obj: dict[str, Any]) -> dict[str, Any]:
    raw = obj.get("metadata")
    return raw if isinstance(raw, dict) else {}


def _status_from_event(event_type: str, sub: dict[str, Any]) -> str:
    raw = sub.get("status")
    if isinstance(raw, str) and raw.strip():
        return raw.strip().lower()
    lowered = event_type.lower()
    for status_name in ("active", "trialing", "past_due", "canceled", "expired"):
        if lowered.endswith(status_name) or f".{status_name}" in lowered:
            return status_name
    if lowered.endswith("payment_failed") or "payment_failed" in lowered:
        return "past_due"
    return ""


def _plan_for_status(provider_status: str) -> str:
    return "pro" if provider_status in {"active", "trialing", "past_due"} else "free"


def _verify_creem_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def _upsert_creem_subscription(
    session: Session,
    *,
    event_type: str,
    obj: dict[str, Any],
) -> None:
    sub = _subscription_payload(obj)
    metadata = _metadata_payload(obj)
    user_id = str(
        metadata.get("user_id")
        or metadata.get("userId")
        or obj.get("user_id")
        or obj.get("userId")
        or ""
    ).strip() or None
    customer_id = str(
        _dig(obj, "customer", "id")
        or obj.get("customer_id")
        or sub.get("customer_id")
        or ""
    ).strip() or None
    subscription_id = str(sub.get("id") or obj.get("subscription_id") or "").strip() or None
    product_id = str(_dig(obj, "product", "id") or sub.get("product_id") or "").strip() or None
    provider_status = _status_from_event(event_type, sub)
    period_end = _parse_period_end(
        sub.get("current_period_end")
        or sub.get("current_period_end_date")
        or obj.get("current_period_end")
    )

    if not any((user_id, customer_id, subscription_id)):
        logger.warning("Creem webhook missing customer/subscription/user identifiers event=%s", event_type)
        return

    clauses = []
    if subscription_id:
        clauses.append(ApiEntitlementRow.provider_subscription_id == subscription_id)
    if user_id:
        clauses.append(ApiEntitlementRow.user_id == user_id)
    if customer_id:
        clauses.append(ApiEntitlementRow.provider_customer_id == customer_id)

    row = None
    for clause in clauses:
        row = session.scalars(
            select(ApiEntitlementRow).where(
                ApiEntitlementRow.provider == "creem",
                clause,
            )
        ).first()
        if row is not None:
            break

    if row is None:
        key_part = user_id or subscription_id or customer_id or "unknown"
        row = ApiEntitlementRow(api_key=f"creem:{key_part}", provider="creem")
        session.add(row)

    row.user_id = user_id or row.user_id
    row.provider = "creem"
    row.provider_customer_id = customer_id or row.provider_customer_id
    row.provider_subscription_id = subscription_id or row.provider_subscription_id
    row.provider_status = provider_status or row.provider_status
    row.provider_price_id = product_id or row.provider_price_id
    row.plan = _plan_for_status(row.provider_status or "")
    row.current_period_end = period_end or row.current_period_end
    if row.provider_status == "past_due" and row.past_due_since is None:
        row.past_due_since = dt.datetime.now(dt.timezone.utc)
    if row.provider_status in {"active", "trialing", "canceled", "expired"}:
        row.past_due_since = None
    row.updated_at = dt.datetime.now(dt.timezone.utc)


@router.post("/api/creem/checkout", response_model=CheckoutResponse)
def create_creem_checkout(
    user: UserRow = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> CheckoutResponse:
    api_key = settings.creem_api_key.strip()
    product_id = settings.creem_product_id_pro.strip()
    if not api_key or not product_id:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "creem_not_configured", "message": "服务端未配置 Creem Checkout。"},
        )

    payload = {
        "product_id": product_id,
        "request_id": user.id,
        "success_url": settings.creem_success_url.strip(),
        "cancel_url": settings.creem_cancel_url.strip(),
        "metadata": {"user_id": user.id, "userId": user.id, "email": user.email},
        "customer": {"email": user.email},
    }
    try:
        resp = httpx.post(
            f"{settings.creem_api_base_url.rstrip('/')}/v1/checkouts",
            headers={"x-api-key": api_key, "Content-Type": "application/json"},
            json={k: v for k, v in payload.items() if v},
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.exception("Creem checkout create failed: %s", exc)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail={"code": "creem_error", "message": str(exc)},
        ) from exc

    url = data.get("checkout_url") or data.get("url")
    if not isinstance(url, str) or not url.strip():
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail={"code": "creem_no_url", "message": "Creem 未返回结账 URL。"},
        )
    return CheckoutResponse(url=url)


@router.api_route("/api/creem/status", methods=["GET", "POST"])
def creem_status(
    user: UserRow = Depends(get_current_user),
    session: Session = Depends(db_session_dep),
) -> dict[str, object]:
    entitlement = resolve_commercial_entitlement(session, user=user)
    row = entitlement.row
    tier = "pro" if entitlement.tier in (CommercialTier.PRO, CommercialTier.ADMIN) else entitlement.tier.value
    return {
        "tier": tier,
        "source": entitlement.source,
        "provider": row.provider if row else None,
        "provider_status": entitlement.provider_status,
        "current_period_end_utc": (
            _aware_utc(entitlement.current_period_end).isoformat()
            if entitlement.current_period_end
            else None
        ),
        "in_grace_period": entitlement.in_grace_period,
        "creem_active": entitlement.source == "creem" and tier == "pro",
    }


@router.post("/api/creem/portal", response_model=PortalResponse)
def create_creem_portal(
    user: UserRow = Depends(get_current_user),
    session: Session = Depends(db_session_dep),
    settings: Settings = Depends(get_settings),
) -> PortalResponse:
    portal_url = settings.creem_portal_url.strip()
    if not portal_url:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "creem_portal_not_configured", "message": "服务端未配置 Creem 客户门户。"},
        )
    row = session.scalars(
        select(ApiEntitlementRow).where(
            ApiEntitlementRow.user_id == user.id,
            ApiEntitlementRow.provider == "creem",
        )
    ).first()
    if row is None or not (row.provider_customer_id or "").strip():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"code": "no_creem_customer", "message": "当前账号尚未绑定 Creem 客户。"},
        )
    return PortalResponse(url=portal_url)


@router.post("/api/creem/webhook")
async def creem_webhook(
    request: Request,
    session: Session = Depends(db_session_dep),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    secret = settings.creem_webhook_secret.strip()
    if not secret:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="webhook_not_configured")

    raw = await request.body()
    signature = request.headers.get("creem-signature") or ""
    if not _verify_creem_signature(raw, signature, secret):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid_signature")

    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid_payload") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid_payload")

    event_id = str(payload.get("id") or payload.get("event_id") or "").strip()
    event_type = _event_type(payload)
    if not event_id:
        digest = hashlib.sha256(raw).hexdigest()[:32]
        event_id = f"creem:{event_type}:{digest}"
    stored_id = f"creem:{event_id}" if not event_id.startswith("creem:") else event_id
    if session.get(PaymentWebhookEventRow, stored_id) is not None:
        return {"ok": "true"}

    try:
        obj = _event_object(payload)
        if any(
            marker in event_type
            for marker in (
                "subscription.",
                "checkout.completed",
                "payment_failed",
            )
        ):
            _upsert_creem_subscription(session, event_type=event_type, obj=obj)
        session.add(PaymentWebhookEventRow(id=stored_id, provider="creem", event_type=event_type))
        session.commit()
    except Exception:
        logger.exception("Creem webhook handler failed event=%s", event_type)
        session.rollback()
        raise

    return {"ok": "true"}
