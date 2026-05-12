"""Persist recent GEX snapshots (Redis hash) for trend charts."""
from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any, Optional

from app.services.cache_service import redis_client_optional

logger = logging.getLogger(__name__)

GEX_HIST_PREFIX = "gex_hist:"


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
        "netGex": float(net),
        "gammaFlip": float(profile["gammaFlip"]) if isinstance(profile.get("gammaFlip"), (int, float)) else None,
        "underlying": float(profile["underlyingPrice"]) if isinstance(profile.get("underlyingPrice"), (int, float)) else None,
        "maxPain": float(profile["maxPain"]) if isinstance(profile.get("maxPain"), (int, float)) else None,
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
            dated.append((dk, json.loads(blob)))
        except (json.JSONDecodeError, TypeError):
            continue
    dated.sort(key=lambda x: x[0])
    trimmed = dated[-limit_days:] if limit_days > 0 else dated
    return [p for _, p in trimmed]


def seed_price_closes(symbol: str, *, days: int = 90) -> list[dict[str, Any]]:
    """Close prices for pairing with sparse GEX (yfinance best-effort)."""
    guard = symbol.strip().upper()
    if not guard:
        return []
    try:
        import yfinance as yf

        t = yf.Ticker(guard)
        hist = t.history(period=f"{days}d", interval="1d", auto_adjust=True)
    except Exception as exc:
        logger.debug("seed_price_closes %s: %s", guard, exc)
        return []
    if hist is None or hist.empty:
        return []
    out: list[dict[str, Any]] = []
    for idx, row in hist.iterrows():
        d_iso = idx.date().isoformat() if hasattr(idx, "date") else str(idx)[:10]
        close = row.get("Close")
        cv = float(close) if isinstance(close, (int, float)) else None
        if cv is None:
            continue
        out.append({"date": d_iso, "close": round(cv, 4)})
    return out[-days:]
