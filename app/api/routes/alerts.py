"""User alert subscriptions API."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import bearer_subscription_optional
from app.db.models import UserAlertRow
from app.db.session import SessionLocal

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


class AlertCreatePayload(BaseModel):
    api_key: str = Field(min_length=8, max_length=256)
    alert_type: str = Field(min_length=1, max_length=32)
    symbol: str = Field(min_length=1, max_length=16)
    threshold: Optional[float] = None


class AlertRowOut(BaseModel):
    id: int
    alert_type: str
    symbol: str
    threshold: Optional[float]
    enabled: bool
    created_at: str


def _resolve_api_key(
    api_key_qs: Optional[str],
    auth_header: Optional[str],
    x_api_key: Optional[str],
) -> str:
    if api_key_qs and api_key_qs.strip():
        return api_key_qs.strip()
    if x_api_key and x_api_key.strip():
        return x_api_key.strip()
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.removeprefix("Bearer ").strip()
        if token:
            return token
    raise HTTPException(status_code=400, detail="api_key is required")


@router.get("")
def list_alerts(
    api_key: Optional[str] = Query(default=None),
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    _: Optional[str] = Depends(bearer_subscription_optional),
) -> dict[str, object]:
    resolved_key = _resolve_api_key(api_key, authorization, x_api_key)
    with SessionLocal() as session:
        rows = session.execute(
            select(UserAlertRow)
            .where(UserAlertRow.api_key == resolved_key)
            .order_by(UserAlertRow.created_at.desc())
        ).scalars().all()
    items = [
        AlertRowOut(
            id=row.id,
            alert_type=row.alert_type,
            symbol=row.symbol,
            threshold=row.threshold,
            enabled=row.enabled,
            created_at=row.created_at.astimezone(timezone.utc).isoformat()
            if isinstance(row.created_at, datetime)
            else "",
        ).model_dump()
        for row in rows
    ]
    return {"success": True, "data": items}


@router.post("")
def create_alert(
    body: AlertCreatePayload,
    _: Optional[str] = Depends(bearer_subscription_optional),
) -> dict[str, object]:
    with SessionLocal() as session:
        row = UserAlertRow(
            api_key=body.api_key.strip(),
            alert_type=body.alert_type.strip(),
            symbol=body.symbol.strip().upper(),
            threshold=body.threshold,
            config_json={},
            enabled=True,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
    return {
        "success": True,
        "data": {
            "id": row.id,
            "alert_type": row.alert_type,
            "symbol": row.symbol,
            "threshold": row.threshold,
            "enabled": row.enabled,
            "created_at": row.created_at.astimezone(timezone.utc).isoformat(),
        },
    }
