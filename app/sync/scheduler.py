"""APScheduler-based data sync scheduler.

All pipelines run on hourly intervals, 24/7. Pipelines are idempotent (safe to re-run)."""
from __future__ import annotations

import logging

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import get_settings

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def _run_safe(fn, name: str) -> None:
    try:
        fn()
    except Exception as exc:
        logger.error("Sync pipeline '%s' failed: %s", name, exc, exc_info=True)


def start_scheduler() -> None:
    global _scheduler
    cfg = get_settings()

    if not cfg.sync_enabled:
        logger.info("Data sync disabled (SYNC_ENABLED=false)")
        return

    if _scheduler and _scheduler.running:
        logger.warning("Scheduler already running")
        return

    tz = pytz.timezone(cfg.sync_timezone)
    _scheduler = BackgroundScheduler(timezone=tz)

    # ── Import pipelines lazily to avoid circular imports ─────────────────────────────────────
    from app.sync.pipelines.options_chain_sync import sync_options_chain_pipeline
    from app.sync.pipelines.stock_quotes_sync import sync_stock_quotes_pipeline
    from app.sync.pipelines.market_data_sync import (
        sync_sectors_pipeline,
        sync_movers_pipeline,
        sync_macro_calendar_pipeline,
        sync_treasury_rates_pipeline,
        sync_earnings_calendar_pipeline,
        sync_news_pipeline,
        sync_analyst_ratings_pipeline,
    )

    # ── High-frequency (every 10 min) ─────────────────────────────────────────────
    _scheduler.add_job(
        lambda: _run_safe(sync_news_pipeline, "news"),
        IntervalTrigger(minutes=10),
        id="news", replace_existing=True, max_instances=1,
    )

    # ── Every 1 hour — options, quotes, market data (24/7) ────────────────────────
    _scheduler.add_job(
        lambda: _run_safe(sync_options_chain_pipeline, "options_chain"),
        IntervalTrigger(hours=1),
        id="options_chain", replace_existing=True, max_instances=1,
    )
    _scheduler.add_job(
        lambda: _run_safe(sync_stock_quotes_pipeline, "stock_quotes"),
        IntervalTrigger(hours=1),
        id="stock_quotes", replace_existing=True, max_instances=1,
    )
    _scheduler.add_job(
        lambda: _run_safe(sync_sectors_pipeline, "sectors"),
        IntervalTrigger(hours=1),
        id="sectors", replace_existing=True, max_instances=1,
    )
    _scheduler.add_job(
        lambda: _run_safe(sync_movers_pipeline, "movers"),
        IntervalTrigger(hours=1),
        id="movers", replace_existing=True, max_instances=1,
    )
    _scheduler.add_job(
        lambda: _run_safe(sync_macro_calendar_pipeline, "macro_calendar"),
        IntervalTrigger(hours=1),
        id="macro_calendar", replace_existing=True, max_instances=1,
    )
    _scheduler.add_job(
        lambda: _run_safe(sync_treasury_rates_pipeline, "treasury"),
        IntervalTrigger(hours=1),
        id="treasury", replace_existing=True, max_instances=1,
    )
    _scheduler.add_job(
        lambda: _run_safe(sync_earnings_calendar_pipeline, "earnings_calendar"),
        IntervalTrigger(hours=1),
        id="earnings_calendar", replace_existing=True, max_instances=1,
    )
    _scheduler.add_job(
        lambda: _run_safe(sync_analyst_ratings_pipeline, "analyst"),
        IntervalTrigger(hours=1),
        id="analyst", replace_existing=True, max_instances=1,
    )

    _scheduler.start()
    logger.info("Data sync scheduler started with %d jobs (hourly, 24/7)", len(_scheduler.get_jobs()))


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Data sync scheduler stopped")


def get_scheduler_status() -> dict:
    if not _scheduler:
        return {"running": False, "jobs": []}
    return {
        "running": _scheduler.running,
        "jobs": [
            {
                "id": job.id,
                "next_run": str(job.next_run_time) if job.next_run_time else None,
            }
            for job in _scheduler.get_jobs()
        ],
    }
