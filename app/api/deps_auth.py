"""JWT user authentication dependencies."""
from __future__ import annotations

from typing import Annotated, Optional

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models_user import UserRow
from app.db.session import db_session_dep
from app.services.jwt_tokens import decode_access_token


def extract_bearer_user_token(authorization: Optional[str]) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        return ""
    return authorization.removeprefix("Bearer ").strip()


async def get_current_user(
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    session: Session = Depends(db_session_dep),
) -> UserRow:
    token = extract_bearer_user_token(authorization)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "unauthorized", "message": "需要 Authorization: Bearer JWT。"},
        )
    payload = decode_access_token(token)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_token", "message": "令牌无效或已过期。"},
        )
    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_token", "message": "令牌载荷无效。"},
        )
    row = session.execute(select(UserRow).where(UserRow.id == sub)).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "user_not_found", "message": "用户不存在。"},
        )
    if row.role == "disabled":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "account_disabled", "message": "账号已禁用。"},
        )
    return row


async def get_current_admin_user(
    user: Annotated[UserRow, Depends(get_current_user)],
) -> UserRow:
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "forbidden", "message": "需要管理员权限。"},
        )
    return user
