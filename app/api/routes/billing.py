"""Stripe Checkout, Customer Portal, and webhooks."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.billing_access import usage_agent_queries_today
from app.config import get_settings
from app.db.models import ApiEntitlementRow, StripeWebhookEventRow
from app.db.session import db_session_dep

logger = logging.getLogger(__name__)

router = APIRouter(tags=["billing"])


class CheckoutBody(BaseModel):
    api_key: str = Field(min_length=8, max_length=200)


class PortalBody(BaseModel):
    api_key: str = Field(min_length=8, max_length=200)


class StatusBody(BaseModel):
    api_key: str = Field(min_length=8, max_length=200)


def _coerce_stripe_dict(obj: object) -> dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        out = to_dict()
        return out if isinstance(out, dict) else {}
    return {}


def _stripe_module():  # lazy import
    import stripe

    return stripe


def _period_end_from_subscription(sub: dict[str, Any]) -> Optional[dt.datetime]:
    raw = sub.get("current_period_end")
    if not isinstance(raw, (int, float)):
        return None
    return dt.datetime.fromtimestamp(float(raw), tz=dt.timezone.utc)


def _apply_subscription_to_customer(
    session: Session,
    *,
    customer_id: str,
    sub: dict[str, Any],
) -> None:
    st = str(sub.get("status") or "")
    period_end = _period_end_from_subscription(sub)
    if st in ("active", "trialing", "past_due"):
        plan = "pro"
    else:
        plan = "free"

    rows = list(
        session.scalars(
            select(ApiEntitlementRow).where(
                ApiEntitlementRow.stripe_customer_id == customer_id,
            ),
        ).all(),
    )
    for row in rows:
        row.plan = plan
        row.current_period_end = period_end
        session.merge(row)
    session.commit()
    logger.info("Stripe subscription sync customer=%s plan=%s", customer_id, plan)


@router.post("/api/billing/checkout")
def create_checkout_session(
    body: CheckoutBody,
) -> dict[str, str]:
    cfg = get_settings()
    key = cfg.stripe_secret_key.strip()
    price = cfg.stripe_price_id_pro.strip()
    if not key or not price:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "stripe_not_configured", "message": "服务端未配置 Stripe。"},
        )

    stripe = _stripe_module()
    stripe.api_key = key

    try:
        sess = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": price, "quantity": 1}],
            success_url=cfg.stripe_success_url.strip() or "https://example.com/settings?billing=success",
            cancel_url=cfg.stripe_cancel_url.strip() or "https://example.com/settings?billing=cancel",
            client_reference_id=body.api_key[:200],
            metadata={"api_key": body.api_key[:200]},
        )
    except Exception as exc:  # stripe error
        logger.exception("Stripe checkout.Session.create failed: %s", exc)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail={"code": "stripe_error", "message": str(exc)},
        ) from exc

    url = sess.get("url") if isinstance(sess, dict) else getattr(sess, "url", None)
    if not url:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail={"code": "stripe_no_url", "message": "Stripe 未返回结账 URL。"},
        )
    return {"url": str(url)}


@router.post("/api/billing/portal")
def create_portal_session(
    body: PortalBody,
    session: Session = Depends(db_session_dep),
) -> dict[str, str]:
    cfg = get_settings()
    key = cfg.stripe_secret_key.strip()
    if not key:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "stripe_not_configured", "message": "服务端未配置 Stripe。"},
        )

    row = session.get(ApiEntitlementRow, body.api_key)
    if row is None or not (row.stripe_customer_id or "").strip():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"code": "no_customer", "message": "该密钥未绑定 Stripe 客户，请先完成结账。"},
        )

    stripe = _stripe_module()
    stripe.api_key = key
    ret = cfg.stripe_portal_return_url.strip() or cfg.stripe_success_url.strip()
    try:
        portal = stripe.billing_portal.Session.create(
            customer=row.stripe_customer_id,
            return_url=ret,
        )
    except Exception as exc:
        logger.exception("Stripe portal create failed: %s", exc)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail={"code": "stripe_error", "message": str(exc)},
        ) from exc

    url = portal.get("url") if isinstance(portal, dict) else getattr(portal, "url", None)
    if not url:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail={"code": "stripe_no_url", "message": "Stripe 未返回门户 URL。"},
        )
    return {"url": str(url)}


@router.post("/api/billing/status")
def billing_status(
    body: StatusBody,
    session: Session = Depends(db_session_dep),
) -> dict[str, object]:
    cfg = get_settings()
    row = session.get(ApiEntitlementRow, body.api_key)

    used = usage_agent_queries_today(session, body.api_key) if row else 0
    lim = max(0, int(cfg.free_tier_daily_agent_queries))
    return {
        "registered": row is not None,
        "plan": row.plan if row else None,
        "stripe_customer_id": row.stripe_customer_id if row else None,
        "current_period_end_utc": (
            row.current_period_end.astimezone(dt.timezone.utc).isoformat()
            if row and row.current_period_end
            else None
        ),
        "agent_queries_today": used,
        "free_daily_limit": lim,
        "stripe_configured": bool(cfg.stripe_secret_key.strip()),
    }


@router.post("/api/billing/webhook")
async def stripe_webhook(
    request: Request,
    session: Session = Depends(db_session_dep),
) -> dict[str, str]:
    cfg = get_settings()
    wh_secret = cfg.stripe_webhook_secret.strip()
    if not wh_secret:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="webhook_not_configured")

    payload = await request.body()
    sig = request.headers.get("stripe-signature") or ""

    stripe = _stripe_module()
    stripe.api_key = cfg.stripe_secret_key.strip() or "sk_dummy"

    try:
        raw = payload if isinstance(payload, (bytes, bytearray)) else bytes(payload)
        event = stripe.Webhook.construct_event(raw, sig, wh_secret)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="invalid_payload",
        ) from exc
    except Exception as exc:
        if type(exc).__name__ != "SignatureVerificationError":
            raise
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="invalid_signature",
        ) from exc

    ev = _coerce_stripe_dict(event)
    eid = str(ev.get("id", ""))
    if not eid:
        return {"ok": "true"}

    existing = session.get(StripeWebhookEventRow, eid)
    if existing is not None:
        return {"ok": "true"}

    etype = str(ev.get("type", ""))
    data_obj = ev.get("data")
    data_d = _coerce_stripe_dict(data_obj) if data_obj is not None else {}
    inner = data_d.get("object")
    inner_d = _coerce_stripe_dict(inner) if inner is not None else {}

    try:
        if etype == "checkout.session.completed":
            api_key = inner_d.get("client_reference_id") or ""
            meta = inner_d.get("metadata") or {}
            if not api_key and isinstance(meta, dict):
                mk = meta.get("api_key")
                api_key = str(mk) if mk else ""
            customer = inner_d.get("customer")
            if api_key and customer:
                row = session.get(ApiEntitlementRow, str(api_key))
                if row is None:
                    row = ApiEntitlementRow(
                        api_key=str(api_key),
                        stripe_customer_id=str(customer),
                        plan="free",
                    )
                    session.add(row)
                else:
                    row.stripe_customer_id = str(customer)
                    session.merge(row)
                logger.info("Checkout completed api_key prefix=%s", str(api_key)[:8])

        elif etype in ("customer.subscription.updated", "customer.subscription.deleted"):
            customer_id = inner_d.get("customer")
            if customer_id:
                _apply_subscription_to_customer(
                    session,
                    customer_id=str(customer_id),
                    sub=inner_d,
                )

        session.add(StripeWebhookEventRow(id=eid))
        session.commit()
    except Exception:
        logger.exception("Stripe webhook handler failed event=%s", etype)
        session.rollback()
        raise

    return {"ok": "true"}
