"""Subscription / shared auth dependencies."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import Header, HTTPException, status

from app.config import get_settings

logger = logging.getLogger(__name__)


async def bearer_subscription_optional(
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
) -> Optional[str]:
    """Verify Bearer subscription token when `subscription_required` is True."""

    settings = get_settings()
    tokens_raw = settings.subscription_tokens.strip()

    if not settings.subscription_required or not tokens_raw:
        return authorization

    if not authorization or not authorization.startswith("Bearer "):
        logger.debug("Rejected request: missing bearer")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "unauthorized", "message": "需要 Authorization: Bearer 订阅令牌。"},
        )

    token_value = authorization.removeprefix("Bearer ").strip()

    allowed = {t.strip() for t in tokens_raw.split(",") if t.strip()}

    if token_value not in allowed:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_token", "message": "令牌无效。"},
        )

    return token_value
