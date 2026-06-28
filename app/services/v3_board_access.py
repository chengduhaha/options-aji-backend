"""Server-side truncation and lock enforcement for v3 leaderboard + GEX APIs."""

from __future__ import annotations

import copy
from typing import Any

from fastapi import HTTPException, status

from app.services.membership import (
    FREE_GEX_SYMBOL,
    FREE_ROW_LIMIT,
    FREE_SYMBOL_MASK_RANKS,
    MEMBER_UNUSUAL_ROW_LIMIT,
    V3Access,
)


def _free_allowed_filters(board_id: str) -> list[str]:
    if board_id == "unusual":
        return ["cp", "topN"]
    return []


def _access_meta(access: V3Access, *, board_id: str) -> dict[str, Any]:
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
                "symbol_mask_ranks": 0,
            }
        return {
            "tier": access.tier,
            "is_member": True,
            "locked": False,
            "row_limit": None,
            "allowed_filters": ["cp", "dte", "moneyness", "topN", "page"],
            "allowed_top_n": [10, 25],
            "max_pages": None,
            "symbol_mask_ranks": 0,
        }

    return {
        "tier": access.tier,
        "is_member": False,
        "locked": False,
        "row_limit": FREE_ROW_LIMIT,
        "allowed_filters": _free_allowed_filters(board_id),
        "allowed_top_n": [10] if board_id == "unusual" else [],
        "max_pages": 1,
        "symbol_mask_ranks": FREE_SYMBOL_MASK_RANKS,
    }


def _mask_row_symbol(row: dict[str, Any], *, rank: int) -> dict[str, Any]:
    masked = copy.deepcopy(row)
    if rank <= FREE_SYMBOL_MASK_RANKS:
        masked["symbol_masked"] = True
        masked["underlying"] = ""
        if "ticker" in masked:
            masked["ticker"] = ""
    else:
        masked["symbol_masked"] = False
    return masked


def _apply_symbol_masking(items: list[Any]) -> list[dict[str, Any]]:
    masked_items: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        row = dict(item) if isinstance(item, dict) else {"rank": index + 1}
        rank = int(row.get("rank") or index + 1)
        masked_items.append(_mask_row_symbol(row, rank=rank))
    return masked_items


def apply_leaderboard_access(
    payload: dict[str, Any],
    *,
    board_id: str,
    access: V3Access,
) -> dict[str, Any]:
    items: list[Any] = list(payload.get("items") or [])

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

    trimmed = items[:FREE_ROW_LIMIT]
    masked_items = _apply_symbol_masking(trimmed)
    return {
        **payload,
        "items": masked_items,
        "total": len(masked_items),
        "locked": False,
        "access": _access_meta(access, board_id=board_id),
    }


def apply_sentiment_access(payload: dict[str, Any], *, access: V3Access) -> dict[str, Any]:
    if access.is_member:
        return {**payload, "access": _access_meta(access, board_id="volume")}

    top_calls = _apply_symbol_masking(list(payload.get("top_calls") or []))[:FREE_ROW_LIMIT]
    top_puts = _apply_symbol_masking(list(payload.get("top_puts") or []))[:FREE_ROW_LIMIT]
    return {
        **payload,
        "top_calls": top_calls,
        "top_puts": top_puts,
        "access": _access_meta(access, board_id="volume"),
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
