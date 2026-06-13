"""Futu OpenAPI quote client for US stocks and options."""
from __future__ import annotations

import logging
import math
import socket
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

import pandas as pd

from app.config import get_settings

logger = logging.getLogger(__name__)

RET_OK = 0
US_INDEX_SYMBOLS = frozenset({"DJI", "IXIC", "NDX", "RUT", "SPX", "VIX"})
_CHAIN_SNAPSHOT_CACHE_TTL_SECONDS = 20.0
_chain_snapshot_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_chain_snapshot_cache_lock = threading.Lock()


def normalize_futu_us_code(symbol: str) -> str:
    """Return a Futu US-market code, preserving already-qualified codes."""

    raw = symbol.strip().upper()
    if not raw:
        return raw
    if raw.startswith("US."):
        return raw
    if raw.startswith("^"):
        return f"US..{raw[1:]}"
    if raw.startswith("."):
        return f"US.{raw}"
    if raw in US_INDEX_SYMBOLS:
        return f"US..{raw}"
    if "." in raw:
        market, remainder = raw.split(".", 1)
        if len(market) <= 2 and remainder:
            return raw
    return f"US.{raw}"


def display_symbol_from_futu_code(code: str) -> str:
    raw = str(code or "").strip().upper()
    if raw.startswith("US.."):
        return f"^{raw[4:]}"
    if raw.startswith("US."):
        return raw[3:]
    return raw


def _as_rows(frame_or_rows: Any) -> list[dict[str, Any]]:
    if frame_or_rows is None:
        return []
    if hasattr(frame_or_rows, "to_dict"):
        return [dict(row) for row in frame_or_rows.to_dict("records")]
    if isinstance(frame_or_rows, list):
        return [dict(row) for row in frame_or_rows if isinstance(row, dict)]
    return []


def _clean_value(value: Any) -> Any:
    if value in ("N/A", "", None):
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def _safe_float(value: Any) -> float | None:
    cleaned = _clean_value(value)
    if cleaned is None:
        return None
    try:
        number = float(cleaned)
    except (TypeError, ValueError):
        return None
    if math.isnan(number):
        return None
    return number


def _safe_int(value: Any) -> int | None:
    number = _safe_float(value)
    if number is None:
        return None
    return int(number)


def _iv_to_decimal(value: Any) -> float | None:
    number = _safe_float(value)
    if number is None:
        return None
    return round(number / 100.0, 6) if number > 2 else round(number, 6)


def _option_type(value: Any) -> str | None:
    raw = str(value or "").strip().upper()
    if raw == "CALL":
        return "call"
    if raw == "PUT":
        return "put"
    return None


def _midpoint(bid: float | None, ask: float | None, last_price: float | None) -> float | None:
    if bid is not None and ask is not None and ask >= bid:
        return round((bid + ask) / 2.0, 6)
    return last_price


def _default_chain_dates(expiration_date: str | None) -> tuple[str, str]:
    if expiration_date:
        return expiration_date, expiration_date
    today = date.today()
    return today.isoformat(), (today + timedelta(days=29)).isoformat()


@dataclass(frozen=True)
class FutuQuoteClient:
    enabled: bool
    host: str = "127.0.0.1"
    port: int = 11111
    snapshot_batch_size: int = 200
    connect_timeout_seconds: float = 0.3
    ctx_factory: Callable[[], Any] | None = None

    @classmethod
    def from_settings(cls) -> "FutuQuoteClient":
        settings = get_settings()
        return cls(
            enabled=settings.futu_enabled,
            host=settings.futu_host,
            port=settings.futu_port,
            snapshot_batch_size=settings.futu_snapshot_batch_size,
            connect_timeout_seconds=settings.futu_connect_timeout_seconds,
        )

    def _new_context(self) -> Any:
        if self.ctx_factory is not None:
            return self.ctx_factory()
        self._ensure_reachable()
        try:
            from futu import OpenQuoteContext
        except Exception as exc:  # pragma: no cover - depends on runtime package install
            raise RuntimeError(f"futu_sdk_unavailable: {exc}") from exc
        return OpenQuoteContext(host=self.host, port=self.port)

    def _ensure_reachable(self) -> None:
        try:
            with socket.create_connection((self.host, self.port), timeout=self.connect_timeout_seconds):
                return
        except OSError as exc:
            raise RuntimeError(f"futu_opend_unreachable: {self.host}:{self.port}") from exc

    def get_stock_quote(self, symbol: str) -> dict[str, Any]:
        display_symbol = symbol.strip().upper()
        if not self.enabled:
            return {"symbol": display_symbol, "error": "futu_not_enabled"}

        futu_code = normalize_futu_us_code(symbol)
        try:
            rows = self.get_market_snapshot_rows([futu_code])
        except Exception as exc:
            logger.warning("Futu stock quote failed %s: %s", symbol, exc)
            return {"symbol": display_symbol, "error": f"futu_quote_failed: {exc}"}
        if not rows:
            return {"symbol": display_symbol, "error": "futu_empty_quote"}

        row = rows[0]
        last_price = _safe_float(row.get("last_price"))
        previous_close = _safe_float(row.get("prev_close_price"))
        change = None
        change_pct = None
        if last_price is not None and previous_close:
            change = round(last_price - previous_close, 6)
            change_pct = round((last_price - previous_close) / previous_close * 100.0, 3)

        return {
            "symbol": display_symbol_from_futu_code(str(row.get("code") or futu_code)),
            "source": "futu",
            "futu_code": str(row.get("code") or futu_code),
            "last_price": last_price,
            "previous_close": previous_close,
            "change": change,
            "change_pct": change_pct,
            "regular_market_open": _safe_float(row.get("open_price")),
            "day_high": _safe_float(row.get("high_price")),
            "day_low": _safe_float(row.get("low_price")),
            "volume": _safe_int(row.get("volume")),
            "market_cap": _safe_int(row.get("total_market_val")),
            "pe": _safe_float(row.get("pe_ratio")),
            "eps": _safe_float(row.get("earning_per_share")),
            "timestamp": _clean_value(row.get("update_time")) or datetime.now(timezone.utc).isoformat(),
        }

    def get_daily_klines(self, symbol: str, *, count: int = 260) -> pd.DataFrame | None:
        """Daily OHLCV history (yfinance-compatible column names) for HV / price charts."""
        if not self.enabled:
            return None
        futu_code = normalize_futu_us_code(symbol)
        context = self._new_context()
        try:
            from futu import AuType, KLType, RET_OK

            today = date.today()
            span_days = max(count + 60, 400)
            start = (today - timedelta(days=span_days)).isoformat()
            end = today.isoformat()
            ret, data, page_req = context.request_history_kline(
                futu_code,
                start=start,
                end=end,
                max_count=max(count, 30),
                ktype=KLType.K_DAY,
                autype=AuType.QFQ,
            )
            if ret != RET_OK or data is None or getattr(data, "empty", True):
                logger.warning("Futu kline empty %s ret=%s", symbol, ret)
                return None
            frames = [data]
            while page_req is not None:
                ret, page_data, page_req = context.request_history_kline(
                    futu_code,
                    start=start,
                    end=end,
                    max_count=max(count, 30),
                    ktype=KLType.K_DAY,
                    autype=AuType.QFQ,
                    page_req_key=page_req,
                )
                if ret != RET_OK or page_data is None or getattr(page_data, "empty", True):
                    break
                frames.append(page_data)
            frame = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0].copy()
            if "time_key" in frame.columns:
                frame = frame.drop_duplicates(subset=["time_key"], keep="last")
                frame = frame.sort_values("time_key")
            rename = {
                "open": "Open",
                "high": "High",
                "low": "Low",
                "close": "Close",
                "volume": "Volume",
            }
            for src, dst in rename.items():
                if src in frame.columns and dst not in frame.columns:
                    frame[dst] = frame[src]
            if "time_key" in frame.columns:
                frame.index = pd.to_datetime(frame["time_key"])
            elif "code" in frame.columns and len(frame) > 0:
                frame.index = pd.RangeIndex(len(frame))
            return frame
        except Exception as exc:
            logger.warning("Futu daily klines failed %s: %s", symbol, exc)
            return None
        finally:
            close = getattr(context, "close", None)
            if callable(close):
                close()

    def get_stock_quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        if not self.enabled:
            return [{"symbol": symbol.strip().upper(), "error": "futu_not_enabled"} for symbol in symbols]
        futu_codes = [normalize_futu_us_code(symbol) for symbol in symbols if symbol.strip()]
        if not futu_codes:
            return []
        rows = self.get_market_snapshot_rows(futu_codes)
        return [self._map_stock_snapshot_row(row) for row in rows]

    def _map_stock_snapshot_row(self, row: dict[str, Any]) -> dict[str, Any]:
        futu_code = str(row.get("code") or "")
        last_price = _safe_float(row.get("last_price"))
        previous_close = _safe_float(row.get("prev_close_price"))
        change = None
        change_pct = None
        if last_price is not None and previous_close:
            change = round(last_price - previous_close, 6)
            change_pct = round((last_price - previous_close) / previous_close * 100.0, 3)
        return {
            "symbol": display_symbol_from_futu_code(futu_code),
            "source": "futu",
            "futu_code": futu_code,
            "last_price": last_price,
            "previous_close": previous_close,
            "change": change,
            "change_pct": change_pct,
            "regular_market_open": _safe_float(row.get("open_price")),
            "day_high": _safe_float(row.get("high_price")),
            "day_low": _safe_float(row.get("low_price")),
            "volume": _safe_int(row.get("volume")),
            "market_cap": _safe_int(row.get("total_market_val")),
            "pe": _safe_float(row.get("pe_ratio")),
            "eps": _safe_float(row.get("earning_per_share")),
            "timestamp": _clean_value(row.get("update_time")) or datetime.now(timezone.utc).isoformat(),
        }

    def get_option_chain_snapshot(
        self,
        symbol: str,
        *,
        expiration_date: str | None = None,
        contract_type: str | None = None,
        strike_price_gte: float | None = None,
        strike_price_lte: float | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        display_symbol = symbol.strip().upper()
        if not self.enabled:
            return {"symbol": display_symbol, "contracts": [], "error": "futu_not_enabled"}

        cache_key = (
            f"{display_symbol}|{expiration_date or ''}|{contract_type or ''}|"
            f"{strike_price_gte}|{strike_price_lte}|{limit}"
        )
        now = time.monotonic()
        with _chain_snapshot_cache_lock:
            cached = _chain_snapshot_cache.get(cache_key)
            if cached and now - cached[0] < _CHAIN_SNAPSHOT_CACHE_TTL_SECONDS:
                return cached[1]

        futu_code = normalize_futu_us_code(symbol)
        start_date, end_date = _default_chain_dates(expiration_date)
        try:
            chain_rows = self.get_option_chain_rows(
                futu_code,
                start=start_date,
                end=end_date,
                contract_type=contract_type,
            )
            filtered_chain_rows = self._filter_chain_rows(
                chain_rows,
                strike_price_gte=strike_price_gte,
                strike_price_lte=strike_price_lte,
                limit=limit,
            )
            option_codes = [str(row.get("code")) for row in filtered_chain_rows if row.get("code")]
            snapshot_rows = self.get_market_snapshot_rows(option_codes)
        except Exception as exc:
            logger.warning("Futu options chain failed %s: %s", symbol, exc)
            result = {"symbol": display_symbol, "contracts": [], "error": f"futu_options_failed: {exc}"}
            return result

        static_by_code = {str(row.get("code")): row for row in filtered_chain_rows if row.get("code")}
        contracts = [
            self._map_option_snapshot(row, static_by_code.get(str(row.get("code")), {}), display_symbol)
            for row in snapshot_rows
        ]
        contracts = [contract for contract in contracts if contract.get("ticker")]
        expirations = sorted({str(contract["expiration_date"]) for contract in contracts if contract.get("expiration_date")})
        result = {
            "symbol": display_symbol,
            "source": "futu",
            "count": len(contracts),
            "expirations": expirations,
            "contracts": contracts,
            "synced_at": datetime.now(timezone.utc).isoformat(),
        }
        with _chain_snapshot_cache_lock:
            _chain_snapshot_cache[cache_key] = (now, result)
        return result

    def get_market_snapshot_rows(self, code_list: list[str]) -> list[dict[str, Any]]:
        if not code_list:
            return []
        context = self._new_context()
        try:
            all_rows: list[dict[str, Any]] = []
            for start_index in range(0, len(code_list), self.snapshot_batch_size):
                batch = code_list[start_index : start_index + self.snapshot_batch_size]
                ret, data = context.get_market_snapshot(batch)
                if ret != RET_OK:
                    raise RuntimeError(str(data))
                all_rows.extend(_as_rows(data))
            return all_rows
        finally:
            close = getattr(context, "close", None)
            if callable(close):
                close()

    def get_option_chain_rows(
        self,
        futu_code: str,
        *,
        start: str,
        end: str,
        contract_type: str | None = None,
    ) -> list[dict[str, Any]]:
        context = self._new_context()
        try:
            option_type = self._futu_option_type(contract_type)
            ret, data = context.get_option_chain(
                futu_code,
                start=start,
                end=end,
                option_type=option_type,
            )
            if ret != RET_OK:
                raise RuntimeError(str(data))
            return _as_rows(data)
        finally:
            close = getattr(context, "close", None)
            if callable(close):
                close()

    def _futu_option_type(self, contract_type: str | None) -> str:
        try:
            from futu import OptionType
        except Exception:
            if contract_type and contract_type.lower() == "call":
                return "CALL"
            if contract_type and contract_type.lower() == "put":
                return "PUT"
            return "ALL"
        if contract_type and contract_type.lower() == "call":
            return OptionType.CALL
        if contract_type and contract_type.lower() == "put":
            return OptionType.PUT
        return OptionType.ALL

    def _filter_chain_rows(
        self,
        rows: list[dict[str, Any]],
        *,
        strike_price_gte: float | None,
        strike_price_lte: float | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        filtered: list[dict[str, Any]] = []
        for row in rows:
            strike = _safe_float(row.get("strike_price"))
            if strike is not None and strike_price_gte is not None and strike < strike_price_gte:
                continue
            if strike is not None and strike_price_lte is not None and strike > strike_price_lte:
                continue
            filtered.append(row)
            if len(filtered) >= limit:
                break
        return filtered

    def _map_option_snapshot(
        self,
        snapshot: dict[str, Any],
        static_row: dict[str, Any],
        fallback_symbol: str,
    ) -> dict[str, Any]:
        ticker = str(snapshot.get("code") or static_row.get("code") or "")
        bid = _safe_float(snapshot.get("bid_price"))
        ask = _safe_float(snapshot.get("ask_price"))
        last_price = _safe_float(snapshot.get("last_price"))
        previous_close = _safe_float(snapshot.get("prev_close_price"))
        day_change = None
        day_change_pct = None
        if last_price is not None and previous_close:
            day_change = round(last_price - previous_close, 6)
            day_change_pct = round((last_price - previous_close) / previous_close * 100.0, 6)

        owner = str(snapshot.get("stock_owner") or static_row.get("stock_owner") or fallback_symbol)
        expiration = _clean_value(snapshot.get("strike_time")) or _clean_value(static_row.get("strike_time"))
        strike = _safe_float(snapshot.get("option_strike_price"))
        if strike is None:
            strike = _safe_float(static_row.get("strike_price"))
        contract_type = _option_type(snapshot.get("option_type")) or _option_type(static_row.get("option_type"))

        return {
            "ticker": ticker,
            "underlying": display_symbol_from_futu_code(owner),
            "contract_type": contract_type,
            "expiration_date": expiration,
            "strike_price": strike,
            "delta": _safe_float(snapshot.get("option_delta")),
            "gamma": _safe_float(snapshot.get("option_gamma")),
            "theta": _safe_float(snapshot.get("option_theta")),
            "vega": _safe_float(snapshot.get("option_vega")),
            "implied_volatility": _iv_to_decimal(snapshot.get("option_implied_volatility")),
            "open_interest": _safe_int(snapshot.get("option_open_interest")),
            "bid": bid,
            "ask": ask,
            "bid_size": _safe_int(snapshot.get("bid_vol")),
            "ask_size": _safe_int(snapshot.get("ask_vol")),
            "midpoint": _midpoint(bid, ask, last_price),
            "day_open": None,
            "day_high": _safe_float(snapshot.get("high_price")),
            "day_low": _safe_float(snapshot.get("low_price")),
            "day_close": last_price,
            "day_volume": _safe_int(snapshot.get("volume")),
            "day_vwap": _safe_float(snapshot.get("avg_price")),
            "day_change": day_change,
            "day_change_pct": day_change_pct,
            "previous_close": previous_close,
            "break_even_price": None,
            "underlying_price": None,
            "last_trade_price": last_price,
            "last_trade_size": None,
            "last_trade_at": None,
            "snapshot_time": datetime.now(timezone.utc),
        }


def get_futu_client() -> FutuQuoteClient:
    return FutuQuoteClient.from_settings()
