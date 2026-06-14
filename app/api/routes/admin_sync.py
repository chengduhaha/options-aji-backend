"""Admin-only manual sync triggers."""
from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel, Field

from app.api.deps_auth import get_current_admin_user
from app.api.routes.congress import _fetch_from_fmp, _upsert_trades
from app.db.models_user import UserRow
from app.db.session import SessionLocal
from app.services.congress_member_profiles import refresh_congress_member_profiles_pipeline
from app.sync.pipelines.options_chain_sync import run_sp500_options_sync

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/sync", tags=["admin-sync"])


class Sp500SyncBody(BaseModel):
    full: bool = Field(default=False, description="Sync all S&P 500 symbols (slow)")
    batch_size: int = Field(default=50, ge=1, le=500)


class CongressProfilesBody(BaseModel):
    limit: int = Field(default=50, ge=1, le=200)


def _run_sp500_sync(*, full: bool, batch_size: int) -> None:
    try:
        outcome = run_sp500_options_sync(full=full, batch_size=batch_size)
        logger.info("admin sp500 options sync done: %s", outcome)
    except Exception:
        logger.exception("admin sp500 options sync failed")


def _run_congress_profiles(*, limit: int) -> None:
    try:
        refresh_congress_member_profiles_pipeline(limit=limit)
        logger.info("admin congress profiles sync done limit=%s", limit)
    except Exception:
        logger.exception("admin congress profiles sync failed")


def _run_congress_trades() -> None:
    session = SessionLocal()
    try:
        senate = _fetch_from_fmp("senate", 500)
        house = _fetch_from_fmp("house", 500)
        _upsert_trades(session, senate, "senate")
        _upsert_trades(session, house, "house")
        logger.info("admin congress trades sync senate=%d house=%d", len(senate), len(house))
    except Exception:
        logger.exception("admin congress trades sync failed")
        session.rollback()
    finally:
        session.close()


@router.post("/sp500-options")
def trigger_sp500_options_sync(
    body: Sp500SyncBody,
    background_tasks: BackgroundTasks,
    _admin: UserRow = Depends(get_current_admin_user),
) -> dict[str, object]:
    background_tasks.add_task(_run_sp500_sync, full=body.full, batch_size=body.batch_size)
    return {
        "status": "started",
        "scope": "sp500_full" if body.full else "sp500_batch",
        "batch_size": body.batch_size,
    }


@router.post("/congress-profiles")
def trigger_congress_profiles_sync(
    body: CongressProfilesBody,
    background_tasks: BackgroundTasks,
    _admin: UserRow = Depends(get_current_admin_user),
) -> dict[str, object]:
    background_tasks.add_task(_run_congress_profiles, limit=body.limit)
    return {"status": "started", "limit": body.limit}


@router.post("/congress-trades")
def trigger_congress_trades_sync(
    background_tasks: BackgroundTasks,
    _admin: UserRow = Depends(get_current_admin_user),
) -> dict[str, object]:
    background_tasks.add_task(_run_congress_trades)
    return {"status": "started"}
