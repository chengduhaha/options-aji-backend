"""Application entry — v2 with PostgreSQL + Redis + data sync scheduler."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes.agent import router as agent_router
from app.api.routes.analyst import router as analyst_router
from app.api.routes.billing import router as billing_router
from app.api.routes.compat import router as compat_router
from app.api.routes.congress import router as congress_router
from app.api.routes.discord_backfill import router as discord_backfill_router
from app.api.routes.etf import router as etf_router
from app.api.routes.events import router as events_router
from app.api.routes.feed_unified import router as feed_unified_router
from app.api.routes.health import router as health_router
from app.api.routes.insider import router as insider_router
from app.api.routes.integration_status import router as integration_router
from app.api.routes.macro import router as macro_router
from app.api.routes.market_dashboard import router as market_dashboard_router
from app.api.routes.market_overview import router as market_overview_router
from app.api.routes.messages import router as messages_router
from app.api.routes.news import router as news_router
from app.api.routes.options import router as options_router
from app.api.routes.scanner import router as scanner_router
from app.api.routes.signals_feed import router as signals_feed_router
from app.api.routes.stock_detail import router as stock_detail_router
from app.api.routes.stock_enhanced import router as stock_enhanced_router
from app.api.routes.strategy_eval import router as strategy_eval_router
from app.api.routes.watchlist import router as watchlist_router
from app.config import get_settings
from app.db.bootstrap import init_db
from app.ingest.discord_bot import parse_channel_ids, run_discord_ingest_forever
from app.ingest.discord_history_rest import run_discord_gap_sync_loop
from app.ingest.feed_enrichment import run_feed_enrichment_loop
from app.logging_setup import apply_noise_filters
from app.sync.scheduler import start_scheduler, stop_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_cfg = get_settings()
apply_noise_filters(enabled=_cfg.suppress_noisy_provider_logs)
logger = logging.getLogger("optionsaji.main")


def _startup_discord_listener() -> None:
    cfg = get_settings()
    channels = parse_channel_ids(cfg.discord_channel_ids)
    if cfg.enable_discord_listener and cfg.discord_bot_token.strip() and bool(channels):
        asyncio.create_task(run_discord_ingest_forever())


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    cfg = get_settings()

    # Ensure data directory exists (for SQLite fallback)
    Path("./data").mkdir(parents=True, exist_ok=True)

    # Initialize DB tables
    init_db()

    # Start Discord ingest (optional)
    _startup_discord_listener()
    if cfg.discord_gap_sync_enabled:
        asyncio.create_task(run_discord_gap_sync_loop())
    if cfg.feed_enrichment_enabled:
        asyncio.create_task(run_feed_enrichment_loop())

    # Start data sync scheduler
    start_scheduler()

    yield

    stop_scheduler()


def create_application() -> FastAPI:
    settings = get_settings()
    origins = ["*"]
    cors_raw = settings.cors_origins.strip()
    if cors_raw and cors_raw != "*":
        origins = [o.strip() for o in cors_raw.split(",") if o.strip()] or origins

    app = FastAPI(
        title=settings.app_name,
        version="0.2.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Core (preserved from v1) ──
    app.include_router(health_router)
    app.include_router(billing_router)
    app.include_router(compat_router)
    app.include_router(agent_router)
    app.include_router(messages_router)
    app.include_router(integration_router)
    app.include_router(market_dashboard_router)
    app.include_router(stock_detail_router)
    app.include_router(scanner_router)
    app.include_router(strategy_eval_router)
    app.include_router(feed_unified_router)
    app.include_router(signals_feed_router)
    app.include_router(discord_backfill_router)
    app.include_router(events_router)

    # ── New v2 routes ──
    app.include_router(options_router)
    app.include_router(market_overview_router)
    app.include_router(macro_router)
    app.include_router(insider_router)
    app.include_router(congress_router)
    app.include_router(etf_router)
    app.include_router(news_router)
    app.include_router(analyst_router)
    app.include_router(stock_enhanced_router)
    app.include_router(watchlist_router)

    return app


app = create_application()


if __name__ == "__main__":
    import os
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8787")),
        reload=False,
    )
