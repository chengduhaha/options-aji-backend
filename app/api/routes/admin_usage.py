"""Admin operational usage endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps_auth import get_current_admin_user
from app.db.models_user import UserRow
from app.db.session import db_session_dep
from app.services.llm_usage import summarize_llm_usage

router = APIRouter(prefix="/api/admin", tags=["admin-usage"])


@router.get("/llm-usage")
def get_llm_usage(
    window: str = Query(default="30d", pattern="^(24h|7d|30d|all|day|week|month|total)$"),
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(db_session_dep),
    _: UserRow = Depends(get_current_admin_user),
) -> dict[str, object]:
    return {"success": True, "data": summarize_llm_usage(session, window=window, limit=limit)}
