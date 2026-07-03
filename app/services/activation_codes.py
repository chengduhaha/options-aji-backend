"""Activation code generation and one-time redemption with stack renewal."""

from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models_user import ActivationCodeRow, UserRow
from app.services.auth_rate_limit import redeem_rate_limited

logger = logging.getLogger(__name__)

DURATION_TIERS: dict[str, int] = {
    "7D": 7,
    "30D": 30,
    "365D": 365,
}

CODE_PATTERN = "OAJI-{tier}-{token}"


def _hash_code(raw: str) -> str:
    return hashlib.sha256(raw.strip().upper().encode("utf-8")).hexdigest()


def _generate_raw_code(duration_tier: str) -> str:
    token = secrets.token_urlsafe(12).replace("-", "").replace("_", "")[:16].upper()
    return CODE_PATTERN.format(tier=duration_tier, token=token)


def _stack_expiry(current: datetime | None, duration_days: int, *, now: datetime) -> datetime:
    base = now
    if current is not None:
        aware = current if current.tzinfo else current.replace(tzinfo=timezone.utc)
        if aware > now:
            base = aware
    return base + timedelta(days=duration_days)


def generate_activation_codes(
    session: Session,
    *,
    duration_tier: str,
    count: int,
    admin: UserRow,
    note: str | None = None,
) -> tuple[str, list[str]]:
    tier = duration_tier.strip().upper()
    if tier not in DURATION_TIERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_tier", "message": f"无效时长档位: {duration_tier}"},
        )
    if count < 1 or count > 500:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_count", "message": "批量数量需在 1–500 之间。"},
        )

    batch_id = str(uuid.uuid4())
    duration_days = DURATION_TIERS[tier]
    codes: list[str] = []
    for _ in range(count):
        for _attempt in range(8):
            raw = _generate_raw_code(tier)
            code_hash = _hash_code(raw)
            exists = session.execute(
                select(ActivationCodeRow.id).where(ActivationCodeRow.code_hash == code_hash)
            ).scalar_one_or_none()
            if exists:
                continue
            prefix = raw[:16]
            row = ActivationCodeRow(
                code_hash=code_hash,
                code_prefix=prefix,
                duration_tier=tier,
                duration_days=duration_days,
                status="available",
                created_by_admin_id=admin.id,
                batch_id=batch_id,
                note=(note or "").strip() or None,
            )
            session.add(row)
            codes.append(raw)
            break
        else:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"code": "generation_failed", "message": "激活码生成失败，请重试。"},
            )

    session.commit()
    logger.info(
        "Generated activation codes batch=%s tier=%s count=%s admin=%s",
        batch_id,
        tier,
        len(codes),
        admin.email,
    )
    return batch_id, codes


def redeem_activation_code(
    session: Session,
    *,
    user: UserRow,
    raw_code: str,
    client_ip: str,
) -> UserRow:
    if redeem_rate_limited(client_ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "rate_limited", "message": "兑换过于频繁，请稍后再试。"},
        )

    normalized = raw_code.strip().upper()
    if not normalized.startswith("OAJI-"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_code", "message": "激活码格式无效。"},
        )

    code_hash = _hash_code(normalized)
    row = session.execute(
        select(ActivationCodeRow)
        .where(ActivationCodeRow.code_hash == code_hash)
        .with_for_update()
    ).scalar_one_or_none()

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "code_not_found", "message": "激活码不存在。"},
        )
    if row.status != "available":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "code_already_used", "message": "激活码已被使用。"},
        )

    now = datetime.now(timezone.utc)
    locked_user = session.execute(
        select(UserRow).where(UserRow.id == user.id).with_for_update()
    ).scalar_one()
    new_expires = _stack_expiry(locked_user.membership_expires_at, row.duration_days, now=now)

    row.status = "redeemed"
    row.redeemed_by_user_id = locked_user.id
    row.redeemed_at = now
    locked_user.membership_expires_at = (
        new_expires if new_expires.tzinfo else new_expires.replace(tzinfo=timezone.utc)
    )
    if row.duration_tier == "7D":
        if (locked_user.membership_kind or "").strip().lower() != "full":
            locked_user.membership_kind = "trial"
    else:
        locked_user.membership_kind = "full"
    session.add(row)
    session.add(locked_user)
    session.commit()
    session.refresh(locked_user)

    logger.info(
        "Activation code redeemed user=%s tier=%s expires=%s code_prefix=%s",
        locked_user.email,
        row.duration_tier,
        new_expires.isoformat(),
        row.code_prefix,
    )
    return locked_user
