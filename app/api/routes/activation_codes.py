"""Admin activation code generation and listing."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps_auth import get_current_admin_user
from app.db.models_user import ActivationCodeRow, UserRow
from app.db.session import db_session_dep
from app.services.activation_codes import DURATION_TIERS, generate_activation_codes
from app.services.auth_rate_limit import generate_codes_rate_limited

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/activation-codes", tags=["admin-activation-codes"])


class GenerateCodesBody(BaseModel):
    duration_tier: Literal["7D", "30D", "365D"]
    count: int = Field(default=1, ge=1, le=500)
    note: Optional[str] = Field(default=None, max_length=256)


class GenerateCodesResponse(BaseModel):
    batch_id: str
    duration_tier: str
    duration_days: int
    count: int
    codes: list[str]


class ActivationCodePublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    code_prefix: str
    duration_tier: str
    duration_days: int
    status: str
    redeemed_by_user_id: Optional[str]
    redeemed_at: Optional[datetime]
    batch_id: Optional[str]
    note: Optional[str]
    created_at: Optional[datetime]


@router.post("/generate", response_model=GenerateCodesResponse)
def admin_generate_codes(
    body: GenerateCodesBody,
    admin: Annotated[UserRow, Depends(get_current_admin_user)],
    session: Session = Depends(db_session_dep),
) -> GenerateCodesResponse:
    if generate_codes_rate_limited(admin.id):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "rate_limited", "message": "生成过于频繁，请稍后再试。"},
        )
    batch_id, codes = generate_activation_codes(
        session,
        duration_tier=body.duration_tier,
        count=body.count,
        admin=admin,
        note=body.note,
    )
    return GenerateCodesResponse(
        batch_id=batch_id,
        duration_tier=body.duration_tier,
        duration_days=DURATION_TIERS[body.duration_tier],
        count=len(codes),
        codes=codes,
    )


@router.get("", response_model=list[ActivationCodePublic])
def admin_list_codes(
    _: Annotated[UserRow, Depends(get_current_admin_user)],
    session: Session = Depends(db_session_dep),
    status_filter: Optional[str] = None,
    limit: int = 100,
) -> list[ActivationCodePublic]:
    q = select(ActivationCodeRow).order_by(ActivationCodeRow.created_at.desc()).limit(min(limit, 500))
    if status_filter:
        q = q.where(ActivationCodeRow.status == status_filter.strip().lower())
    rows = session.execute(q).scalars().all()
    return [ActivationCodePublic.model_validate(row) for row in rows]
