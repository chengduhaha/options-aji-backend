"""Earnings history: FMP calendar + yfinance price windows (T−1 → T+1 trading days)."""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Optional

import httpx
import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EarningsHistoryEntry:
    date: str
    eps: Optional[float]
    eps_estimated: Optional[float]
    revenue: Optional[float]
    #: Close-to-close % from last trading day before event to first trading day strictly after.
    price_window_move_pct: Optional[float]
    iv_crush_pct: Optional[float]  # placeholder until historical chain snapshots exist
    source: str


def _iso(d: dt.date) -> str:
    return d.isoformat()


def _window_move_pct(symbol: str, event_day: dt.date) -> Optional[float]:
    try:
        t = yf.Ticker(symbol)
        start = event_day - dt.timedelta(days=21)
        end = event_day + dt.timedelta(days=21)
        h = t.history(start=_iso(start), end=_iso(end + dt.timedelta(days=1)), auto_adjust=False)
        if h is None or h.empty or "Close" not in h.columns:
            return None
        closes = h["Close"].dropna()
        if closes.empty:
            return None
        pairs: list[tuple[dt.date, float]] = []
        for ts, v in closes.items():
            ts_pd = pd.Timestamp(ts)
            if ts_pd.tzinfo is not None:
                d = ts_pd.tz_convert("UTC").date()
            else:
                d = ts_pd.date()
            pairs.append((d, float(v)))
        pairs.sort(key=lambda x: x[0])
        before = [p for p in pairs if p[0] < event_day]
        after = [p for p in pairs if p[0] > event_day]
        if not before or not after:
            return None
        c0 = before[-1][1]
        c1 = after[0][1]
        if c0 <= 0:
            return None
        return round((c1 / c0 - 1.0) * 100.0, 4)
    except Exception as exc:
        logger.debug("window move %s %s: %s", symbol, event_day, exc)
        return None


def fetch_fmp_historical_earnings(
    *,
    symbol: str,
    api_key: str,
    limit: int = 12,
) -> list[dict[str, object]]:
    key = api_key.strip()
    if not key:
        return []
    sym = symbol.strip().upper()
    url = f"https://financialmodelingprep.com/api/v3/historical/earning_calendar/{sym}?apikey={key}"
    try:
        with httpx.Client(timeout=45.0) as client:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("FMP earnings %s: %s", sym, exc)
        return []
    if not isinstance(data, list):
        return []
    trimmed: list[dict[str, object]] = []
    for row in data[: max(1, limit)]:
        if isinstance(row, dict):
            trimmed.append(row)
    return trimmed


def build_earnings_history(
    *,
    symbol: str,
    fmp_api_key: str,
    limit: int = 8,
) -> tuple[list[EarningsHistoryEntry], str]:
    sym = symbol.strip().upper()
    fmp_rows = fetch_fmp_historical_earnings(symbol=sym, api_key=fmp_api_key, limit=limit)
    out: list[EarningsHistoryEntry] = []
    note = ""

    if fmp_rows:
        for row in fmp_rows:
            raw_date = row.get("date")
            if not isinstance(raw_date, str):
                continue
            try:
                if "-" in raw_date:
                    ed = dt.date.fromisoformat(raw_date[:10])
                else:
                    ed = dt.datetime.strptime(raw_date, "%Y-%m-%d").date()
            except ValueError:
                continue

            def _f(name: str) -> Optional[float]:
                v = row.get(name)
                if isinstance(v, (int, float)):
                    return float(v)
                return None

            move = _window_move_pct(sym, ed)
            out.append(
                EarningsHistoryEntry(
                    date=ed.isoformat(),
                    eps=_f("eps"),
                    eps_estimated=_f("epsEstimated"),
                    revenue=_f("revenue"),
                    price_window_move_pct=move,
                    iv_crush_pct=None,
                    source="FMP+yfinance",
                ),
            )
        note = "历史行：FMP historical earning_calendar；涨跌幅为事件日前后首个可用交易日的收盘价变动（%）。IV crush 需历史期权链。"
        return out, note

    # yfinance-only fallback (dates + moves, no EPS detail)
    try:
        t = yf.Ticker(sym)
        eds = getattr(t, "earnings_dates", None)
        if eds is None or getattr(eds, "empty", True):
            return [], "未配置 FMP_API_KEY 且无 yfinance 财报日期表。"
        for ts in list(getattr(eds, "index", []))[:limit]:
            if hasattr(ts, "date"):
                ed = ts.date()
            elif isinstance(ts, dt.datetime):
                ed = ts.date()
            else:
                continue
            move = _window_move_pct(sym, ed)
            out.append(
                EarningsHistoryEntry(
                    date=ed.isoformat(),
                    eps=None,
                    eps_estimated=None,
                    revenue=None,
                    price_window_move_pct=move,
                    iv_crush_pct=None,
                    source="yfinance_only",
                ),
            )
        note = "降级：仅 yfinance 财报日期；EPS 需配置 FMP_API_KEY。"
        return out, note
    except Exception as exc:
        logger.warning("earnings fallback %s: %s", sym, exc)
        return [], "无法读取财报历史。"
