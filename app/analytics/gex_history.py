"""Persist recent GEX snapshots (Redis hash) for trend charts."""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
from typing import Any, Optional

from app.services.cache_service import redis_client_optional
from app.tools.yf_helpers import yf_ticker

logger = logging.getLogger(__name__)

GEX_HIST_PREFIX = "gex_hist:"


def _finite_float(value: object) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def _history_key(symbol: str) -> str:
    return f"{GEX_HIST_PREFIX}{symbol.upper()}"


def record_gex_snapshot(symbol: str, profile: dict[str, Any]) -> None:
    """Store one row per UTC calendar day (overwrite same day)."""
    r = redis_client_optional()
    if r is None:
        return
    net = profile.get("netGex")
    if net is None or not isinstance(net, (int, float)):
        return
    day_key = dt.datetime.now(dt.timezone.utc).date().isoformat()
    point = {
        "date": day_key,
        "netGex": _finite_float(net),
        "gammaFlip": _finite_float(profile.get("gammaFlip")),
        "underlying": _finite_float(profile.get("underlyingPrice")),
        "maxPain": _finite_float(profile.get("maxPain")),
        "regime": profile.get("regime"),
        "expiration": profile.get("expiration"),
    }
    try:
        r.hset(_history_key(symbol), day_key, json.dumps(point, default=str))
        r.expire(_history_key(symbol), 86400 * 365)
    except Exception as exc:
        logger.debug("record_gex_snapshot %s: %s", symbol, exc)


def list_gex_history(symbol: str, *, limit_days: int = 120) -> list[dict[str, Any]]:
    r = redis_client_optional()
    if r is None:
        return []
    try:
        raw = r.hgetall(_history_key(symbol.upper()))
    except Exception as exc:
        logger.debug("list_gex_history %s: %s", symbol, exc)
        return []
    if not raw:
        return []

    dated: list[tuple[str, dict[str, Any]]] = []
    for dk, blob in raw.items():
        try:
            point = json.loads(blob)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(point, dict):
            continue
        sanitized = {
            "date": str(point.get("date") or dk)[:10],
            "netGex": _finite_float(point.get("netGex")),
            "gammaFlip": _finite_float(point.get("gammaFlip")),
            "underlying": _finite_float(point.get("underlying")),
            "maxPain": _finite_float(point.get("maxPain")),
            "regime": point.get("regime"),
            "expiration": point.get("expiration"),
        }
        if sanitized["netGex"] is None:
            continue
        dated.append((dk, sanitized))
    dated.sort(key=lambda x: x[0])
    trimmed = dated[-limit_days:] if limit_days > 0 else dated
    return [p for _, p in trimmed]


def seed_price_closes(symbol: str, *, days: int = 90) -> list[dict[str, Any]]:
    """Close prices for pairing with sparse GEX (yfinance best-effort)."""
    guard = symbol.strip().upper()
    if not guard:
        return []
    try:
        t = yf_ticker(guard)
        hist = t.history(period=f"{days}d", interval="1d", auto_adjust=True)
    except Exception as exc:
        logger.debug("seed_price_closes %s: %s", guard, exc)
        return []
    if hist is None or hist.empty:
        return []
    out: list[dict[str, Any]] = []
    for idx, row in hist.iterrows():
        d_iso = idx.date().isoformat() if hasattr(idx, "date") else str(idx)[:10]
        cv = _finite_float(row.get("Close"))
        if cv is None:
            continue
        out.append({"date": d_iso, "close": round(cv, 4)})
    return out[-days:]
