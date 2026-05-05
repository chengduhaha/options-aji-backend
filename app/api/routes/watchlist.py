"""User watchlist API routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, delete
from sqlalchemy.orm import Session

from app.api.deps import db_session_dep
from app.db.models import WatchlistRow

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


class AddSymbolRequest(BaseModel):
    symbol: str
    api_key: str = "default"


@router.get("")
def get_watchlist(api_key: str = "default", db: Session = Depends(db_session_dep)):
    rows = db.execute(
        select(WatchlistRow).where(WatchlistRow.api_key == api_key)
    ).scalars().all()
    return {"symbols": [r.symbol for r in rows]}


@router.post("")
def add_to_watchlist(req: AddSymbolRequest, db: Session = Depends(db_session_dep)):
    sym = req.symbol.upper()
    existing = db.execute(
        select(WatchlistRow).where(
            WatchlistRow.api_key == req.api_key,
            WatchlistRow.symbol == sym,
        )
    ).scalar_one_or_none()
    if existing:
        return {"status": "already_exists", "symbol": sym}
    db.add(WatchlistRow(api_key=req.api_key, symbol=sym))
    db.commit()
    return {"status": "added", "symbol": sym}


@router.delete("/{symbol}")
def remove_from_watchlist(symbol: str, api_key: str = "default", db: Session = Depends(db_session_dep)):
    sym = symbol.upper()
    db.execute(
        delete(WatchlistRow).where(
            WatchlistRow.api_key == api_key,
            WatchlistRow.symbol == sym,
        )
    )
    db.commit()
    return {"status": "removed", "symbol": sym}
