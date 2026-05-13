"""JWT access tokens for user sessions."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import JWTError, jwt

from app.config import get_settings

logger = logging.getLogger(__name__)

ALGORITHM = "HS256"


def _secret() -> str:
    s = get_settings().jwt_secret_key.strip()
    if s:
        return s
    logger.warning("JWT_SECRET_KEY unset; using insecure dev fallback")
    return "optionsaji-dev-insecure-jwt-secret"


def create_access_token(*, user_id: str, email: str, role: str) -> str:
    cfg = get_settings()
    now = datetime.now(timezone.utc)
    exp = now + timedelta(hours=max(1, int(cfg.jwt_expire_hours)))
    payload = {
        "sub": user_id,
        "email": email,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    return jwt.encode(payload, _secret(), algorithm=ALGORITHM)


def decode_access_token(token: str) -> Optional[dict[str, object]]:
    try:
        return jwt.decode(token, _secret(), algorithms=[ALGORITHM])
    except JWTError as exc:
        logger.debug("JWT decode failed: %s", exc)
        return None
