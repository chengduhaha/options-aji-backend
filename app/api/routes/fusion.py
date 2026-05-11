"""Fusion API — combine multi-dimensional data into structured insights."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select, func

from app.analytics.market_fusion import analyze_market_regime, FusionInput
from app.analytics.gex_compute import compute_gex_profile
from app.clients.fmp_client import get_fmp_client
from app.config import get_settings
from app.db.session import db_session_dep
from app.db.models import OptionsSnapshotRow
from app.services.cache_service import TTL_HOT, cache_get, cache_set

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/fusion", tags=["fusion"])


@router.get("/market")
def fusion_market_overview(_=Depends(db_session_dep)):
    """Combine all market dimensions into a structured regime analysis (cached)."""
    cached = cache_get("fusion:market")
    if cached:
        return cached

    cfg = get_settings()
    fmp = get_fmp_client() if cfg.fmp_api_key else None
    data = FusionInput()

    if fmp:
        try:
            spy = fmp.get_quote("SPY")
            if spy:
                data.spot_price = spy.get("price")
                data.change_pct = spy.get("changePercentage")
        except Exception:
            pass
        try:
            vix = fmp.get_quote("^VIX")
            if vix:
                data.vix = vix.get("price")
                data.vix_change_pct = vix.get("changePercentage")
        except Exception:
            pass

    try:
        gex = compute_gex_profile("SPY")
        if isinstance(gex, dict) and not gex.get("error"):
            data.net_gex_bn = gex.get("netGex")
            data.gex_regime = gex.get("regime")
            data.gamma_flip = gex.get("gammaFlip")
            data.max_pain = gex.get("maxPain")
    except Exception:
        pass

    try:
        session = __import__("app.db.session", fromlist=["SessionLocal"]).SessionLocal()
        try:
            calls = session.execute(
                select(func.sum(OptionsSnapshotRow.day_volume))
                .where(OptionsSnapshotRow.contract_type == "call")
            ).scalar() or 0
            puts = session.execute(
                select(func.sum(OptionsSnapshotRow.day_volume))
                .where(OptionsSnapshotRow.contract_type == "put")
            ).scalar() or 0
            data.pcr_volume = (puts / calls) if calls > 0 else None
        finally:
            session.close()
    except Exception:
        pass

    analysis = analyze_market_regime(data)
    result = {
        "symbol": "SPY",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "analysis": {
            "regime": analysis.regime,
            "signal": analysis.signal,
            "confidence": analysis.confidence,
            "summary_zh": analysis.summary_zh,
            "strategy_bias": analysis.strategy_bias,
            "risk_factors": analysis.risk_factors,
        },
    }
    cache_set("fusion:market", result, ttl=TTL_HOT)
    return result


@router.get("/stock/{symbol}")
def fusion_stock(symbol: str, db=Depends(db_session_dep)):
    """Multi-dimensional fusion for a single stock."""
    sym = symbol.upper()
    cached = cache_get(f"fusion:stock:{sym}")
    if cached:
        return cached

    data = FusionInput(symbol=sym)
    cfg = get_settings()
    fmp = get_fmp_client() if cfg.fmp_api_key else None

    if fmp:
        try:
            q = fmp.get_quote(sym)
            if q:
                data.spot_price = q.get("price")
                data.change_pct = q.get("changePercentage")
        except Exception:
            pass

    try:
        gex = compute_gex_profile(sym)
        if isinstance(gex, dict) and not gex.get("error"):
            data.gex_regime = gex.get("regime")
            data.net_gex_bn = gex.get("netGex")
            data.gamma_flip = gex.get("gammaFlip")
            data.max_pain = gex.get("maxPain")
    except Exception:
        pass

    try:
        row = db.execute(
            select(OptionsSnapshotRow)
            .where(OptionsSnapshotRow.underlying_ticker == sym)
            .order_by(OptionsSnapshotRow.expiration_date)
            .limit(1)
        ).scalar_one_or_none()
        if row and row.implied_volatility:
            data.atm_iv = row.implied_volatility * 100
    except Exception:
        pass

    analysis = analyze_market_regime(data)
    result = {
        "symbol": sym,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "analysis": {
            "regime": analysis.regime,
            "signal": analysis.signal,
            "confidence": analysis.confidence,
            "summary_zh": analysis.summary_zh,
            "strategy_bias": analysis.strategy_bias,
            "risk_factors": analysis.risk_factors,
        },
    }
    cache_set(f"fusion:stock:{sym}", result, ttl=TTL_HOT)
    return result