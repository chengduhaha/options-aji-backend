"""LLM token/cost accounting for admin operations monitoring."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import LlmUsageRow


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _since_window(window: str) -> datetime | None:
    normalized = window.strip().lower()
    if normalized in {"all", "total"}:
        return None
    if normalized in {"24h", "day"}:
        return _now() - timedelta(hours=24)
    if normalized in {"7d", "week"}:
        return _now() - timedelta(days=7)
    if normalized in {"30d", "month"}:
        return _now() - timedelta(days=30)
    return _now() - timedelta(days=7)


def _usage_int(usage: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return max(0, int(value))
    return 0


def extract_usage_tokens(payload: dict[str, Any]) -> tuple[int, int, int, float | None]:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return 0, 0, 0, None
    input_tokens = _usage_int(usage, "prompt_tokens", "input_tokens")
    output_tokens = _usage_int(usage, "completion_tokens", "output_tokens")
    total_tokens = _usage_int(usage, "total_tokens")
    if total_tokens <= 0:
        total_tokens = input_tokens + output_tokens
    raw_cost = usage.get("cost") or usage.get("cost_usd")
    cost = float(raw_cost) if isinstance(raw_cost, (int, float)) else None
    return input_tokens, output_tokens, total_tokens, cost


def record_llm_usage(
    session: Session,
    *,
    provider: str,
    model: str,
    source: str,
    success: bool,
    input_tokens: int = 0,
    output_tokens: int = 0,
    total_tokens: int | None = None,
    cost_usd: float | None = None,
    latency_ms: int | None = None,
    error_code: str | None = None,
) -> LlmUsageRow:
    total = int(total_tokens if total_tokens is not None else input_tokens + output_tokens)
    row = LlmUsageRow(
        provider=provider[:32],
        model=model[:128],
        source=source[:128],
        success=success,
        input_tokens=max(0, int(input_tokens)),
        output_tokens=max(0, int(output_tokens)),
        total_tokens=max(0, total),
        cost_usd=cost_usd,
        latency_ms=latency_ms,
        error_code=(error_code or "")[:64] or None,
    )
    session.add(row)
    session.commit()
    return row


def _empty_bucket() -> dict[str, Any]:
    return {
        "calls": 0,
        "success_calls": 0,
        "failed_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
    }


def _add(bucket: dict[str, Any], row: LlmUsageRow) -> None:
    bucket["calls"] += 1
    bucket["success_calls"] += 1 if row.success else 0
    bucket["failed_calls"] += 0 if row.success else 1
    bucket["input_tokens"] += int(row.input_tokens or 0)
    bucket["output_tokens"] += int(row.output_tokens or 0)
    bucket["total_tokens"] += int(row.total_tokens or 0)
    bucket["cost_usd"] = round(float(bucket["cost_usd"] or 0) + float(row.cost_usd or 0), 6)


def summarize_llm_usage(session: Session, *, window: str = "30d", limit: int = 100) -> dict[str, Any]:
    since = _since_window(window)
    stmt = select(LlmUsageRow).order_by(LlmUsageRow.created_at.desc()).limit(max(1, min(limit, 500)))
    if since is not None:
        stmt = stmt.where(LlmUsageRow.created_at >= since)
    rows = list(session.execute(stmt).scalars().all())
    total = _empty_bucket()
    by_provider: dict[str, dict[str, Any]] = {}
    by_source: dict[str, dict[str, Any]] = {}
    for row in rows:
        _add(total, row)
        provider_bucket = by_provider.setdefault(row.provider or "unknown", _empty_bucket())
        _add(provider_bucket, row)
        source_bucket = by_source.setdefault(row.source or "unknown", _empty_bucket())
        _add(source_bucket, row)
    return {
        "window": window,
        "total": total,
        "by_provider": by_provider,
        "by_source": by_source,
        "recent": [
            {
                "id": row.id,
                "provider": row.provider,
                "model": row.model,
                "source": row.source,
                "success": row.success,
                "input_tokens": row.input_tokens,
                "output_tokens": row.output_tokens,
                "total_tokens": row.total_tokens,
                "cost_usd": row.cost_usd,
                "latency_ms": row.latency_ms,
                "error_code": row.error_code,
                "created_at": row.created_at.astimezone(timezone.utc).isoformat() if row.created_at else None,
            }
            for row in rows[:50]
        ],
    }
