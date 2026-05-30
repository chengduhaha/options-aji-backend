"""Site-wide sidebar visibility (admin configure, users read)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps_auth import get_current_admin_user, get_current_user
from app.db.models_user import UserRow
from app.db.session import db_session_dep
from app.services.site_nav import (
    KNOWN_NAV_IDS,
    NAV_GROUP_CHILDREN,
    load_visibility,
    merge_visibility_update,
    save_visibility,
)

router = APIRouter(tags=["site-nav"])


class NavVisibilityResponse(BaseModel):
    visibility: dict[str, bool]
    known_ids: list[str] = Field(default_factory=lambda: list(KNOWN_NAV_IDS))
    groups: dict[str, list[str]] = Field(
        default_factory=lambda: {k: list(v) for k, v in NAV_GROUP_CHILDREN.items()}
    )


class NavVisibilityUpdateBody(BaseModel):
    visibility: dict[str, bool] = Field(default_factory=dict)


@router.get("/api/site/nav-visibility", response_model=NavVisibilityResponse)
def get_nav_visibility(
    session: Session = Depends(db_session_dep),
    _: UserRow = Depends(get_current_user),
) -> NavVisibilityResponse:
    vis = load_visibility(session)
    return NavVisibilityResponse(visibility=vis)


@router.put("/api/admin/nav-visibility", response_model=NavVisibilityResponse)
def put_nav_visibility(
    body: NavVisibilityUpdateBody,
    admin: UserRow = Depends(get_current_admin_user),
    session: Session = Depends(db_session_dep),
) -> NavVisibilityResponse:
    current = load_visibility(session)
    merged = merge_visibility_update(current, body.visibility)
    vis = save_visibility(session, merged, updated_by_user_id=admin.id)
    return NavVisibilityResponse(visibility=vis)
