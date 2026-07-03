"""Optional JWT + v3 membership resolution for public APIs."""

from __future__ import annotations

from typing import Annotated, Optional

from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps_auth import extract_bearer_user_token
from app.db.models_user import UserRow
from app.db.session import db_session_dep
from app.services.jwt_tokens import decode_access_token
from app.services.membership import V3Access, resolve_v3_access


async def get_optional_user(
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    session: Session = Depends(db_session_dep),
) -> UserRow | None:
    token = extract_bearer_user_token(authorization)
    if not token:
        return None
    payload = decode_access_token(token)
    if payload is None:
        return None
    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub.strip():
        return None
    row = session.execute(select(UserRow).where(UserRow.id == sub)).scalar_one_or_none()
    if row is None or row.role == "disabled":
        return None
    return row


async def get_v3_access(
    user: Annotated[UserRow | None, Depends(get_optional_user)],
    session: Session = Depends(db_session_dep),
) -> V3Access:
    return resolve_v3_access(user, session=session)
