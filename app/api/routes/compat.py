"""Legacy-style routes used by OptionsAji Next.js (`/market/:sym`, `/gex/:sym`)."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.analytics.gex_history import record_gex_snapshot
from app.tools.openbb_tools import build_default_toolkit

router = APIRouter(tags=["compat"])


@router.get("/market/{symbol}", response_model=None)
def market_sidebar(symbol: str):
    toolkit = build_default_toolkit()
    blob = toolkit.frontend_market_bar(symbol.strip().upper())
    if blob.get("error"):
        err = blob.get("error")
        return JSONResponse(
            status_code=502,
            content={"symbol": blob.get("symbol"), "error": err},
        )
    return blob


@router.get("/gex/{symbol}", response_model=None)
def gex_dashboard(symbol: str):
    toolkit = build_default_toolkit()
    blob = toolkit.get_gex(symbol.strip().upper())
    if blob.get("available") is False:
        return JSONResponse(
            status_code=503,
            content={
                "symbol": blob.get("symbol"),
                "code": "gex_upstream_missing",
                "message": blob.get("hint"),
            },
        )
    if blob.get("error"):
        return JSONResponse(
            status_code=502,
            content={"symbol": blob.get("symbol"), "error": blob.get("error")},
        )
    out = dict(blob)
    out.pop("available", None)
    sym = symbol.strip().upper()
    if isinstance(out.get("netGex"), (int, float)):
        record_gex_snapshot(sym, out)
    return out
