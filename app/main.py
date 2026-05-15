"""Application entry — v2 with PostgreSQL + Redis + data sync scheduler."""
from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes.agent import router as agent_router
from app.api.routes.alerts import router as alerts_router
from app.api.routes.auth import router as auth_router
from app.api.routes.billing import router as billing_router
from app.api.routes.brief import router as brief_router
from app.api.routes.earnings_symbol import router as earnings_symbol_router
from app.api.routes.feed_ai import router as feed_ai_router
from app.api.routes.feed_unified import router as feed_unified_router
from app.api.routes.health import router as health_router
from app.api.routes.integration_status import router as integration_router
from app.api.routes.market_dashboard import router as market_dashboard_router
from app.api.routes.options import router as options_router
from app.api.routes.profile import router as profile_router
from app.api.routes.scanner import router as scanner_router
from app.api.routes.social import router as social_router
from app.api.routes.stock_detail import router as stock_detail_router
from app.api.routes.strategy_eval import router as strategy_eval_router
from app.api.schemas.response import ApiError, ApiFailure
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
    allow_credentials = bool(settings.cors_allow_credentials)
    if allow_credentials and "*" in origins:
        logger.warning(
            "CORS misconfiguration: allow_credentials=True with wildcard origin; forcing allow_credentials=False."
        )
        allow_credentials = False

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
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_observability_middleware(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        request.state.request_id = request_id
        started = asyncio.get_running_loop().time()
        response = await call_next(request)
        elapsed_ms = int((asyncio.get_running_loop().time() - started) * 1000)
        response.headers["x-request-id"] = request_id
        logger.info(
            "request_id=%s method=%s path=%s status=%s elapsed_ms=%s",
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
        return response

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        code = "http_error"
        if isinstance(exc.detail, str) and exc.detail.strip():
            message = exc.detail
        else:
            message = "Request failed."
        payload = ApiFailure(
            error=ApiError(
                code=code,
                message=message,
                details={"status_code": exc.status_code},
                request_id=request_id,
            ),
        )
        return JSONResponse(status_code=exc.status_code, content=payload.model_dump())

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        payload = ApiFailure(
            error=ApiError(
                code="validation_error",
                message="Invalid request payload.",
                details={"errors": exc.errors()},
                request_id=request_id,
            ),
        )
        return JSONResponse(status_code=422, content=payload.model_dump())

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        logger.exception("Unhandled server error request_id=%s", request_id, exc_info=exc)
        payload = ApiFailure(
            error=ApiError(
                code="internal_error",
                message="Internal server error.",
                details={"type": type(exc).__name__},
                request_id=request_id,
            ),
        )
        return JSONResponse(status_code=500, content=payload.model_dump())

    # ── Core 2.0 routes ──
    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(billing_router)
    app.include_router(agent_router)
    app.include_router(alerts_router)
    app.include_router(integration_router)
    app.include_router(market_dashboard_router)
    app.include_router(stock_detail_router)
    app.include_router(options_router)
    app.include_router(profile_router)
    app.include_router(earnings_symbol_router)
    app.include_router(scanner_router)
    app.include_router(social_router)
    app.include_router(strategy_eval_router)
    app.include_router(feed_unified_router)
    app.include_router(feed_ai_router)
    app.include_router(brief_router)

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
