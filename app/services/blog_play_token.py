"""Short-lived tickets for gated blog video streaming."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import JWTError, jwt

from app.config import Settings, get_settings
from app.services.jwt_tokens import ALGORITHM, _secret

logger = logging.getLogger(__name__)

PLAY_TOKEN_TYPE = "blog_play"


def _ttl_seconds(settings: Settings | None = None) -> int:
    cfg = settings or get_settings()
    return max(60, int(cfg.blog_play_token_ttl_seconds))


def create_play_token(
    *,
    attachment_id: str,
    preview: bool,
    settings: Settings | None = None,
) -> tuple[str, datetime]:
    cfg = settings or get_settings()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=_ttl_seconds(cfg))
    payload = {
        "typ": PLAY_TOKEN_TYPE,
        "aid": attachment_id,
        "preview": preview,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(payload, _secret(), algorithm=ALGORITHM)
    return token, expires_at


def decode_play_token(token: str) -> Optional[dict[str, object]]:
    try:
        payload = jwt.decode(token, _secret(), algorithms=[ALGORITHM])
    except JWTError as exc:
        logger.debug("Play token decode failed: %s", exc)
        return None
    if payload.get("typ") != PLAY_TOKEN_TYPE:
        return None
    attachment_id = payload.get("aid")
    if not isinstance(attachment_id, str) or not attachment_id.strip():
        return None
    preview = bool(payload.get("preview"))
    return {
        "attachment_id": attachment_id,
        "preview": preview,
        "exp": payload.get("exp"),
    }
