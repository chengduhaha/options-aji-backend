"""Application entry."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes.agent import router as agent_router
from app.api.routes.billing import router as billing_router
from app.api.routes.compat import router as compat_router
from app.api.routes.discord_backfill import router as discord_backfill_router
from app.api.routes.events import router as events_router
from app.api.routes.feed_unified import router as feed_unified_router
from app.api.routes.health import router as health_router
from app.api.routes.integration_status import router as integration_router
from app.api.routes.market_dashboard import router as market_dashboard_router
from app.api.routes.messages import router as messages_router
from app.api.routes.scanner import router as scanner_router
from app.api.routes.signals_feed import router as signals_feed_router
from app.api.routes.stock_detail import router as stock_detail_router
from app.api.routes.strategy_eval import router as strategy_eval_router
from app.config import get_settings
from app.db.bootstrap import init_db
from app.ingest.discord_bot import parse_channel_ids, run_discord_ingest_forever
from app.ingest.discord_history_rest import run_discord_gap_sync_loop
from app.ingest.feed_enrichment import run_feed_enrichment_loop
from app.logging_setup import apply_noise_filters

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

    ready = cfg.enable_discord_listener and cfg.discord_bot_token.strip() and bool(channels)
    if not ready:
        return

    asyncio.create_task(run_discord_ingest_forever())


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    Path("./data").mkdir(parents=True, exist_ok=True)
    init_db()
    _startup_discord_listener()
    asyncio.create_task(run_discord_gap_sync_loop())
    asyncio.create_task(run_feed_enrichment_loop())

    yield


def create_application() -> FastAPI:
    settings = get_settings()
    origins = ["*"]

    cors_raw = settings.cors_origins.strip()
    if cors_raw and cors_raw != "*":
        origins = [o.strip() for o in cors_raw.split(",") if o.strip()] or origins

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(billing_router)
    app.include_router(health_router)
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
