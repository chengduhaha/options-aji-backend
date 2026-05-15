"""User profile endpoints (push settings)."""

from __future__ import annotations

from datetime import timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from app.api.deps import bearer_subscription_optional
from app.db.models import UserPushSettingRow, UserScannerTemplateRow
from app.db.session import SessionLocal

router = APIRouter(prefix="/api/profile", tags=["profile"])


def _resolve_api_key(
    api_key: Optional[str],
    authorization: Optional[str],
    x_api_key: Optional[str],
) -> str:
    if api_key and api_key.strip():
        return api_key.strip()
    if x_api_key and x_api_key.strip():
        return x_api_key.strip()
    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
        if token:
            return token
    raise HTTPException(status_code=400, detail="api_key is required")


class PushSettingsPayload(BaseModel):
    api_key: str = Field(min_length=8, max_length=256)
    push_discord: bool = True
    push_telegram: bool = False
    push_email: bool = False
    keywords: str = ""


class ScannerTemplateConfigPayload(BaseModel):
    preset: str = Field(min_length=1, max_length=40)
    query_text: str = Field(default="", max_length=300)
    symbol_scope: str = Field(default="", max_length=2000)
    dte_min: str = Field(default="", max_length=12)
    dte_max: str = Field(default="", max_length=12)
    delta_min: str = Field(default="", max_length=12)
    delta_max: str = Field(default="", max_length=12)
    iv_min: str = Field(default="", max_length=12)
    iv_max: str = Field(default="", max_length=12)
    expiration_scope: str = Field(default="next_three", max_length=24)
    sort_field: str = Field(default="volOiRatio", max_length=24)
    sort_direction: str = Field(default="desc", max_length=8)


class ScannerTemplateUpsertPayload(BaseModel):
    api_key: str = Field(min_length=8, max_length=256)
    template_id: Optional[int] = Field(default=None, ge=1)
    name: str = Field(min_length=1, max_length=80)
    config: ScannerTemplateConfigPayload


@router.get("/push-settings")
def get_push_settings(
    api_key: Optional[str] = None,
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    _: Optional[str] = Depends(bearer_subscription_optional),
) -> dict[str, object]:
    resolved = _resolve_api_key(api_key, authorization, x_api_key)
    with SessionLocal() as session:
        row = session.execute(
            select(UserPushSettingRow).where(UserPushSettingRow.api_key == resolved).limit(1)
        ).scalar_one_or_none()
    if row is None:
        return {
            "success": True,
            "data": {
                "push_discord": True,
                "push_telegram": False,
                "push_email": False,
                "keywords": "",
                "updated_at": None,
            },
        }
    return {
        "success": True,
        "data": {
            "push_discord": row.push_discord,
            "push_telegram": row.push_telegram,
            "push_email": row.push_email,
            "keywords": row.keywords,
            "updated_at": row.updated_at.astimezone(timezone.utc).isoformat(),
        },
    }


@router.post("/push-settings")
def upsert_push_settings(
    body: PushSettingsPayload,
    _: Optional[str] = Depends(bearer_subscription_optional),
) -> dict[str, object]:
    with SessionLocal() as session:
        row = session.execute(
            select(UserPushSettingRow).where(UserPushSettingRow.api_key == body.api_key).limit(1)
        ).scalar_one_or_none()
        if row is None:
            row = UserPushSettingRow(api_key=body.api_key)
            session.add(row)
        row.push_discord = body.push_discord
        row.push_telegram = body.push_telegram
        row.push_email = body.push_email
        row.keywords = body.keywords.strip()
        session.commit()
        session.refresh(row)
    return {
        "success": True,
        "data": {
            "push_discord": row.push_discord,
            "push_telegram": row.push_telegram,
            "push_email": row.push_email,
            "keywords": row.keywords,
            "updated_at": row.updated_at.astimezone(timezone.utc).isoformat(),
        },
    }


@router.get("/scanner-templates")
def list_scanner_templates(
    api_key: Optional[str] = None,
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    _: Optional[str] = Depends(bearer_subscription_optional),
) -> dict[str, object]:
    resolved = _resolve_api_key(api_key, authorization, x_api_key)
    with SessionLocal() as session:
        rows = session.execute(
            select(UserScannerTemplateRow)
            .where(UserScannerTemplateRow.api_key == resolved)
            .order_by(UserScannerTemplateRow.updated_at.desc())
            .limit(20)
        ).scalars().all()
    data = [
        {
            "id": row.id,
            "name": row.name,
            "config": row.config_json,
            "updated_at": row.updated_at.astimezone(timezone.utc).isoformat(),
        }
        for row in rows
    ]
    return {"success": True, "data": data}


@router.post("/scanner-templates")
def upsert_scanner_template(
    body: ScannerTemplateUpsertPayload,
    _: Optional[str] = Depends(bearer_subscription_optional),
) -> dict[str, object]:
    with SessionLocal() as session:
        row: Optional[UserScannerTemplateRow] = None
        if body.template_id is not None:
            row = session.execute(
                select(UserScannerTemplateRow).where(
                    UserScannerTemplateRow.id == body.template_id,
                    UserScannerTemplateRow.api_key == body.api_key,
                )
            ).scalar_one_or_none()
        if row is None:
            row = UserScannerTemplateRow(api_key=body.api_key, name=body.name.strip())
            session.add(row)
        row.name = body.name.strip()
        row.config_json = body.config.model_dump()
        session.commit()
        session.refresh(row)
    return {
        "success": True,
        "data": {
            "id": row.id,
            "name": row.name,
            "config": row.config_json,
            "updated_at": row.updated_at.astimezone(timezone.utc).isoformat(),
        },
    }


@router.delete("/scanner-templates/{template_id}")
def delete_scanner_template(
    template_id: int,
    api_key: Optional[str] = None,
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    _: Optional[str] = Depends(bearer_subscription_optional),
) -> dict[str, object]:
    resolved = _resolve_api_key(api_key, authorization, x_api_key)
    with SessionLocal() as session:
        deleted = session.execute(
            delete(UserScannerTemplateRow).where(
                UserScannerTemplateRow.id == template_id,
                UserScannerTemplateRow.api_key == resolved,
            )
        ).rowcount
        session.commit()
    return {"success": True, "deleted": int(deleted or 0)}
