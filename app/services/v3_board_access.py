"""Server-side truncation and lock enforcement for v3 leaderboard + GEX APIs."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status

from app.services.membership import (
    FREE_GEX_SYMBOL,
    FREE_PREVIEW_BOARDS,
    FREE_ROW_LIMIT,
    LOCKED_BOARDS,
    MEMBER_UNUSUAL_ROW_LIMIT,
    V3Access,
)


def _access_meta(access: V3Access, *, board_id: str, locked: bool = False) -> dict[str, Any]:
    if access.is_member:
        if board_id == "unusual":
            return {
                "tier": access.tier,
                "is_member": True,
                "locked": False,
                "row_limit": MEMBER_UNUSUAL_ROW_LIMIT,
                "allowed_filters": ["cp", "dte", "moneyness", "topN", "page"],
                "allowed_top_n": [10, 25],
                "max_pages": 10,
            }
        return {
            "tier": access.tier,
            "is_member": True,
            "locked": False,
            "row_limit": None,
            "allowed_filters": ["cp", "dte", "moneyness", "topN", "page"],
            "allowed_top_n": [10, 25],
            "max_pages": None,
        }

    if locked or board_id in LOCKED_BOARDS:
        return {
            "tier": access.tier,
            "is_member": False,
            "locked": True,
            "row_limit": 0,
            "allowed_filters": [],
            "allowed_top_n": [],
            "max_pages": 0,
        }

    if board_id in FREE_PREVIEW_BOARDS:
        meta: dict[str, Any] = {
            "tier": access.tier,
            "is_member": False,
            "locked": False,
            "row_limit": FREE_ROW_LIMIT,
            "allowed_filters": ["cp", "topN"] if board_id == "unusual" else [],
            "allowed_top_n": [10] if board_id == "unusual" else [],
            "max_pages": 1 if board_id == "unusual" else 1,
        }
        return meta

    return {
        "tier": access.tier,
        "is_member": False,
        "locked": True,
        "row_limit": 0,
        "allowed_filters": [],
        "allowed_top_n": [],
        "max_pages": 0,
    }


def apply_leaderboard_access(
    payload: dict[str, Any],
    *,
    board_id: str,
    access: V3Access,
) -> dict[str, Any]:
    items: list[Any] = list(payload.get("items") or [])
    locked = board_id in LOCKED_BOARDS and not access.is_member

    if locked:
        return {
            **payload,
            "items": [],
            "total": 0,
            "locked": True,
            "access": _access_meta(access, board_id=board_id, locked=True),
        }

    if access.is_member:
        if board_id == "unusual":
            items = items[:MEMBER_UNUSUAL_ROW_LIMIT]
        return {
            **payload,
            "items": items,
            "total": len(items),
            "locked": False,
            "access": _access_meta(access, board_id=board_id),
        }

    if board_id in FREE_PREVIEW_BOARDS:
        trimmed = items[:FREE_ROW_LIMIT]
        return {
            **payload,
            "items": trimmed,
            "total": len(trimmed),
            "locked": False,
            "access": _access_meta(access, board_id=board_id),
        }

    return {
        **payload,
        "items": [],
        "total": 0,
        "locked": True,
        "access": _access_meta(access, board_id=board_id, locked=True),
    }


def enforce_gex_symbol_access(symbol: str, access: V3Access) -> None:
    sym = symbol.strip().upper()
    if access.is_member:
        return
    if sym != FREE_GEX_SYMBOL:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "membership_required",
                "message": f"免费用户仅可查看 {FREE_GEX_SYMBOL} GEX，升级会员解锁全部标的。",
                "allowed_symbol": FREE_GEX_SYMBOL,
            },
        )
