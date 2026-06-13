"""Runtime probes for cross-market integrations."""
from __future__ import annotations

import asyncio
import time

import httpx
from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.clients.fmp_client import get_fmp_client
from app.config import get_settings
from app.cross_market import ibkr_connection as ibkr_conn
from app.cross_market.massive_utils import get_options_chain_results
from app.cross_market.polymarket_client import PolymarketClient
from app.cross_market.redis_cache import ping_redis as cm_ping_redis
from app.cross_market.xpoz_client import XpozClient
from app.db.session import engine as sync_engine

router = APIRouter(prefix="/api/cross-market/diagnostics", tags=["diagnostics"])


class SourceRow(BaseModel):
    source: str
    ok: bool
    credential_configured: bool
    latency_ms: float = Field(description="Round-trip for the probe call")
    http_status: int | None = None
    error_short: str | None = None
    records_or_rows: int | None = None
    sample_top_level_keys: list[str] = Field(default_factory=list)
    probe_description: str


class DataSourcesReport(BaseModel):
    symbol: str
    postgres: SourceRow
    redis: SourceRow
    polymarket: SourceRow
    massive: SourceRow
    xpoz: SourceRow
    fmp_quote: SourceRow
    fmp_insider: SourceRow
    ibkr: SourceRow


def _keys_sample(obj: object, limit: int = 12) -> list[str]:
    if isinstance(obj, dict):
        return [str(k) for k in list(obj.keys())[:limit]]
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        return [str(k) for k in list(obj[0].keys())[:limit]]
    return []


@router.get("/data-sources", response_model=DataSourcesReport)
async def data_sources_probe(
    symbol: str = Query(default="AAPL", min_length=1, max_length=8),
) -> DataSourcesReport:
    sym = symbol.upper().strip()
    settings = get_settings()

    pg_t0 = time.perf_counter()
    pg_ok = False
    pg_err: str | None = None
    pg_counts: dict[str, int] = {}
    try:
        with sync_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            pg_ok = True
            for tbl in ("event_snapshots", "arbitrage_signals"):
                try:
                    r = conn.execute(text(f"SELECT COUNT(*) FROM {tbl}"))
                    pg_counts[tbl] = int(r.scalar() or 0)
                except Exception:
                    pg_counts[tbl] = 0
    except Exception as exc:
        pg_err = f"{type(exc).__name__}: {exc}"[:200]
    pg_lat = (time.perf_counter() - pg_t0) * 1000
    pg_row = SourceRow(
        source="SQLAlchemy sync engine",
        ok=pg_ok,
        credential_configured=bool(settings.database_url.strip()),
        latency_ms=round(pg_lat, 2),
        error_short=pg_err,
        records_or_rows=sum(pg_counts.values()) if pg_ok else None,
        sample_top_level_keys=list(pg_counts.keys()),
        probe_description="SELECT 1; optional COUNT on cross-market snapshot tables",
    )

    redis_t0 = time.perf_counter()
    redis_ok = await cm_ping_redis()
    redis_lat = (time.perf_counter() - redis_t0) * 1000
    redis_row = SourceRow(
        source="Redis",
        ok=redis_ok,
        credential_configured=bool(settings.redis_url.strip()),
        latency_ms=round(redis_lat, 2),
        error_short=None if redis_ok else "ping_failed",
        probe_description="cross_market redis ping",
    )

    poly_t0 = time.perf_counter()
    poly_ok = False
    poly_err: str | None = None
    poly_n = 0
    poly_keys: list[str] = []
    pclient = PolymarketClient()
    try:
        poly = await pclient.search_markets(active=True, limit=5)
        poly_n = len(poly)
        poly_ok = poly_n > 0
        if poly:
            poly_keys = _keys_sample(poly[0])
        if not poly_ok:
            poly_err = "empty_markets_list"
    except Exception as exc:
        poly_err = f"{type(exc).__name__}: {exc}"[:200]
    finally:
        await pclient.close()
    poly_lat = (time.perf_counter() - poly_t0) * 1000
    poly_row = SourceRow(
        source="Polymarket (Gamma)",
        ok=poly_ok,
        credential_configured=True,
        latency_ms=round(poly_lat, 2),
        error_short=poly_err,
        records_or_rows=poly_n,
        sample_top_level_keys=poly_keys,
        probe_description="GET /markets active=true limit=5",
    )

    m_key = bool(settings.massive_api_key.strip())
    mass_t0 = time.perf_counter()
    mass_ok = False
    mass_err: str | None = None
    mass_status: int | None = None
    mass_n = 0
    mass_keys: list[str] = []
    try:
        if not m_key:
            mass_err = "missing_MASSIVE_API_KEY"
        else:
            payload = await get_options_chain_results(sym)
            rows = payload.get("results") if isinstance(payload, dict) else None
            if isinstance(rows, list):
                mass_n = len(rows)
                mass_ok = True
                if rows:
                    mass_keys = _keys_sample(rows[0])
            else:
                mass_err = "unexpected_payload_shape"
    except httpx.HTTPStatusError as exc:
        mass_status = exc.response.status_code
        mass_err = f"HTTP {mass_status}"[:120]
    except Exception as exc:
        mass_err = f"{type(exc).__name__}: {exc}"[:200]
    mass_lat = (time.perf_counter() - mass_t0) * 1000
    mass_row = SourceRow(
        source="Massive",
        ok=mass_ok and m_key,
        credential_configured=m_key,
        latency_ms=round(mass_lat, 2),
        http_status=mass_status,
        error_short=mass_err,
        records_or_rows=mass_n if m_key else 0,
        sample_top_level_keys=mass_keys,
        probe_description=f"option chain snapshot underlying={sym}",
    )

    x_key = bool(settings.xpoz_api_key.strip())
    x_t0 = time.perf_counter()
    x_ok = False
    x_err: str | None = None
    x_status: int | None = None
    x_keys: list[str] = []
    x_mentions = 0
    xclient = XpozClient()
    try:
        if not x_key:
            x_err = "missing_XPOZ_API_KEY"
        else:
            data = await xclient.get_ticker_sentiment(ticker=sym, window_hours=24)
            if isinstance(data, dict) and data:
                x_ok = True
                x_keys = _keys_sample(data)
                x_mentions = int(data.get("mention_count") or 0)
            else:
                x_err = "empty_response"
    except httpx.HTTPStatusError as exc:
        x_status = exc.response.status_code
        x_err = f"HTTP {x_status}"[:120]
    except Exception as exc:
        x_err = f"{type(exc).__name__}: {exc}"[:200]
    finally:
        await xclient.close()
    x_lat = (time.perf_counter() - x_t0) * 1000
    xpoz_row = SourceRow(
        source="Xpoz MCP",
        ok=x_ok,
        credential_configured=x_key,
        latency_ms=round(x_lat, 2),
        http_status=x_status,
        error_short=x_err,
        records_or_rows=x_mentions if x_ok else 0,
        sample_top_level_keys=x_keys,
        probe_description="MCP getTwitterPostsByKeywords",
    )

    f_key = bool(settings.fmp_api_key.strip())
    fq_t0 = time.perf_counter()
    fq_ok = False
    fq_err: str | None = None
    fq_keys: list[str] = []

    def _fq() -> None:
        nonlocal fq_ok, fq_err, fq_keys
        row = get_fmp_client().get_quote(sym)
        if row:
            fq_ok = True
            fq_keys = _keys_sample(row)
        else:
            fq_err = "empty_quote"

    try:
        if not f_key:
            fq_err = "missing_FMP_API_KEY"
        else:
            await asyncio.to_thread(_fq)
    except Exception as exc:
        fq_err = f"{type(exc).__name__}: {exc}"[:200]
    fq_lat = (time.perf_counter() - fq_t0) * 1000

    fi_t0 = time.perf_counter()
    fi_ok = False
    fi_err: str | None = None
    fi_n = 0
    fi_keys: list[str] = []

    def _fi() -> None:
        nonlocal fi_ok, fi_err, fi_n, fi_keys
        trades = get_fmp_client().get_insider_trades(sym)
        fi_n = len(trades)
        fi_ok = fi_n > 0
        if trades:
            fi_keys = _keys_sample(trades[0])

    try:
        if not f_key:
            fi_err = "missing_FMP_API_KEY"
        else:
            await asyncio.to_thread(_fi)
    except Exception as exc:
        fi_err = f"{type(exc).__name__}: {exc}"[:200]
    fi_lat = (time.perf_counter() - fi_t0) * 1000

    fmp_q_row = SourceRow(
        source="FMP (quote)",
        ok=fq_ok and f_key,
        credential_configured=f_key,
        latency_ms=round(fq_lat, 2),
        error_short=fq_err,
        records_or_rows=1 if fq_ok else 0,
        sample_top_level_keys=fq_keys,
        probe_description=f"stable/quote symbol={sym}",
    )
    fmp_i_row = SourceRow(
        source="FMP (insider-trading)",
        ok=fi_ok and f_key and fi_err is None,
        credential_configured=f_key,
        latency_ms=round(fi_lat, 2),
        error_short=fi_err,
        records_or_rows=fi_n,
        sample_top_level_keys=fi_keys,
        probe_description=f"stable/insider-trading/search symbol={sym}",
    )

    ib_t0 = time.perf_counter()
    ib_ok = False
    ib_err: str | None = None
    ib_enabled = ibkr_conn.ibkr_is_enabled()
    if not ib_enabled:
        ib_err = "ibkr_disabled"
    else:
        ib_time = await ibkr_conn.ibkr_req_current_time_async()
        if ib_time is not None:
            ib_ok = True
        elif not ibkr_conn.ibkr_is_connected():
            ib_err = "ibkr_not_connected"
        else:
            ib_err = "reqCurrentTime_failed"
    ib_lat = (time.perf_counter() - ib_t0) * 1000
    ibkr_row = SourceRow(
        source="IBKR (Gateway API)",
        ok=ib_ok,
        credential_configured=ib_enabled,
        latency_ms=round(ib_lat, 2),
        error_short=ib_err,
        records_or_rows=1 if ib_ok else 0,
        sample_top_level_keys=(["server_time_unix"] if ib_ok else []),
        probe_description="ibkr_req_current_time_async",
    )

    return DataSourcesReport(
        symbol=sym,
        postgres=pg_row,
        redis=redis_row,
        polymarket=poly_row,
        massive=mass_row,
        xpoz=xpoz_row,
        fmp_quote=fmp_q_row,
        fmp_insider=fmp_i_row,
        ibkr=ibkr_row,
    )
