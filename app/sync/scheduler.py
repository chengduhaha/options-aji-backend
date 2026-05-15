"""APScheduler-based data sync scheduler.

Pipelines run on a fixed schedule during market hours.
All pipelines are idempotent (safe to re-run).
"""
from __future__ import annotations

import logging
from datetime import datetime

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import get_settings

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def _market_hours_guard() -> bool:
    """Return True during extended US market hours (8am–6pm ET Mon–Fri)."""
    tz = pytz.timezone("America/New_York")
    now = datetime.now(tz)
    if now.weekday() >= 5:  # Sat/Sun
        return False
    return 8 <= now.hour < 18


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
    from app.services.social_sentiment import ingest_all_social_pipelines

    # ── Every 15 min during market hours ──────────────────────────────────────────────
    _scheduler.add_job(
        lambda: _market_hours_guard() and _run_safe(sync_options_chain_pipeline, "options_chain"),
        IntervalTrigger(minutes=15),
        id="options_chain", replace_existing=True, max_instances=1,
    )
    _scheduler.add_job(
        lambda: _market_hours_guard() and _run_safe(sync_stock_quotes_pipeline, "stock_quotes"),
        IntervalTrigger(minutes=15),
        id="stock_quotes", replace_existing=True, max_instances=1,
    )
    _scheduler.add_job(
        lambda: _market_hours_guard() and _run_safe(sync_sectors_pipeline, "sectors"),
        IntervalTrigger(minutes=15),
        id="sectors", replace_existing=True, max_instances=1,
    )

    # ── Every 5 min during market hours ────────────────────────────────────────────────
    _scheduler.add_job(
        lambda: _market_hours_guard() and _run_safe(sync_movers_pipeline, "movers"),
        IntervalTrigger(minutes=5),
        id="movers", replace_existing=True, max_instances=1,
    )

    # ── Every 10 min (news) ────────────────────────────────────────────────────────────
    _scheduler.add_job(
        lambda: _run_safe(sync_news_pipeline, "news"),
        IntervalTrigger(minutes=10),
        id="news", replace_existing=True, max_instances=1,
    )
    _scheduler.add_job(
        lambda: _run_safe(ingest_all_social_pipelines, "social_sentiment"),
        IntervalTrigger(minutes=10),
        id="social_sentiment", replace_existing=True, max_instances=1,
    )

    # ── Daily 6:30 AM ET (pre-market) ───────────────────────────────────────────────
    _scheduler.add_job(
        lambda: _run_safe(sync_macro_calendar_pipeline, "macro_calendar"),
        CronTrigger(hour=6, minute=30, day_of_week="mon-fri", timezone=tz),
        id="macro_calendar", replace_existing=True,
    )
    _scheduler.add_job(
        lambda: _run_safe(sync_earnings_calendar_pipeline, "earnings_calendar"),
        CronTrigger(hour=6, minute=45, day_of_week="mon-fri", timezone=tz),
        id="earnings_calendar", replace_existing=True,
    )
    _scheduler.add_job(
        lambda: _run_safe(sync_analyst_ratings_pipeline, "analyst"),
        CronTrigger(hour=7, minute=0, day_of_week="mon-fri", timezone=tz),
        id="analyst", replace_existing=True,
    )

    # ── Daily 5:00 PM ET (post-market) ───────────────────────────────────────────────
    _scheduler.add_job(
        lambda: _run_safe(sync_treasury_rates_pipeline, "treasury"),
        CronTrigger(hour=17, minute=0, day_of_week="mon-fri", timezone=tz),
        id="treasury", replace_existing=True,
    )

    _scheduler.start()
    logger.info("Data sync scheduler started with %d jobs", len(_scheduler.get_jobs()))


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