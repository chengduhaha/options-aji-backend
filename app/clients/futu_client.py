"""Futu OpenAPI quote client for US stocks and options."""
from __future__ import annotations

import logging
import math
import re
import socket
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterator

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


_OPTION_NAME_STRIKE_RE = re.compile(r"\s(\d+(?:\.\d+)?)[CP]$", re.IGNORECASE)
# Futu US OCC: US.{SYMBOL}{YYMMDD}{C|P}{STRIKE_MILLIS}
# STRIKE_MILLIS = round(strike * 1000), up to 8 digits. Futu strips leading AND trailing zeros.
_OPTION_CODE_STRIKE_RE = re.compile(r"[CP](\d+)$", re.IGNORECASE)


def _parse_strike_from_option_name(option_name: str) -> float | None:
    """Parse strike from Futu option_name, e.g. 'QQQ 260717 711.00C'."""
    match = _OPTION_NAME_STRIKE_RE.search(str(option_name or "").strip())
    if not match:
        return None
    return _safe_float(match.group(1))


def _strike_millis_candidates_from_code(code: str) -> list[float]:
    """Strike from Futu OCC suffix: millis = round(strike * 1000), leading zeros stripped."""
    match = _OPTION_CODE_STRIKE_RE.search(str(code or "").strip().upper())
    if not match:
        return []
    digits = match.group(1)
    if not digits or not digits.isdigit():
        return []
    # Pad leading zeros to 8 millis digits (Futu strips them), then / 1000.
    strike = int(digits.zfill(8)) / 1000.0
    return [strike] if strike > 0 else []


def _parse_strike_from_option_code(code: str) -> float | None:
    """Default OCC parse (shortest millis form) — prefer _resolve_option_strike for boards."""
    candidates = _strike_millis_candidates_from_code(code)
    return candidates[0] if candidates else None


def _spot_from_row(row: dict[str, Any]) -> float | None:
    underlying_info = row.get("underlying")
    if isinstance(underlying_info, dict):
        spot = _safe_float(underlying_info.get("price"))
        if spot is not None and spot > 0:
            return spot
    return _safe_float(row.get("underlying_price"))


def _contract_type_from_row(row: dict[str, Any]) -> str | None:
    option_type_raw = row.get("option_type")
    if option_type_raw == 1:
        return "call"
    if option_type_raw == 2:
        return "put"
    mapped = _option_type(row.get("option_type"))
    if mapped:
        return mapped
    name = str(row.get("option_name") or row.get("code") or "").upper()
    if name.endswith("C"):
        return "call"
    if name.endswith("P"):
        return "put"
    return None


def _strike_matches_option_quote(strike: float, row: dict[str, Any]) -> bool:
    """True when strike equals option last/mid — likely polluted, not a real strike."""
    price = _safe_float(row.get("price"))
    premium = _safe_float(row.get("premium"))
    mid = _safe_float(row.get("mid_price"))
    for quote in (price, premium, mid):
        if quote is not None and quote > 0 and abs(strike - quote) / max(quote, 1.0) < 0.02:
            return True
    return False


def _is_strike_sane(strike: float, spot: float | None, contract_type: str | None) -> bool:
    if strike <= 0:
        return False
    if spot is None or spot <= 0:
        return True
    if spot > 50 and abs(strike - spot) / spot > 0.5:
        return False
    if spot > 50:
        if contract_type == "call" and strike < spot * 0.05:
            return False
        if contract_type == "put" and strike > spot * 20:
            return False
    return True


def _pick_best_strike(
    candidates: list[float],
    *,
    spot: float | None,
    contract_type: str | None,
    row: dict[str, Any],
    option_name_strike: float | None = None,
) -> float | None:
    unique = sorted({c for c in candidates if c is not None and c > 0})
    sane = [
        s
        for s in unique
        if not _strike_matches_option_quote(s, row) and _is_strike_sane(s, spot, contract_type)
    ]
    if not sane:
        return None
    if (
        option_name_strike is not None
        and option_name_strike in sane
        and not _strike_matches_option_quote(option_name_strike, row)
        and _is_strike_sane(option_name_strike, spot, contract_type)
    ):
        return option_name_strike
    if spot is not None and spot > 0:
        return min(sane, key=lambda s: abs(s - spot))
    return sane[0]


def _resolve_option_strike(row: dict[str, Any]) -> float | None:
    """Resolve contract strike: OCC code → option_name → strike_price, with spot sanity."""
    option_name = str(row.get("option_name") or "").strip()
    code = str(row.get("code") or "").strip()
    spot = _spot_from_row(row)
    contract_type = _contract_type_from_row(row)

    strike_from_name = _parse_strike_from_option_name(option_name)

    candidates: list[float] = []
    candidates.extend(_strike_millis_candidates_from_code(code))
    if strike_from_name is not None:
        candidates.append(strike_from_name)
    strike_from_field = _safe_float(row.get("strike_price"))
    if strike_from_field is not None:
        candidates.append(strike_from_field)

    return _pick_best_strike(
        candidates,
        spot=spot,
        contract_type=contract_type,
        row=row,
        option_name_strike=strike_from_name,
    )


def _compute_moneyness(
    strike: float | None,
    spot: float | None,
    contract_type: str | None,
    *,
    in_the_money: bool | None = None,
) -> str:
    if strike is not None and spot is not None and spot > 0:
        rel = abs(strike - spot) / spot
        if rel < 0.005:
            return "ATM"
        if contract_type == "call":
            return "ITM" if strike < spot else "OTM"
        if contract_type == "put":
            return "ITM" if strike > spot else "OTM"
    if in_the_money is True:
        return "ITM"
    if in_the_money is False:
        return "OTM"
    return "OTM"


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

    @contextmanager
    def _borrow_context(self) -> Iterator[Any]:
        """Acquire a pooled OpenQuoteContext (or test ctx_factory) for one operation."""
        if self.ctx_factory is not None:
            context = self.ctx_factory()
            try:
                yield context
            finally:
                close = getattr(context, "close", None)
                if callable(close):
                    close()
            return

        if not self.enabled:
            self._ensure_reachable()
            try:
                from futu import OpenQuoteContext
            except Exception as exc:  # pragma: no cover
                raise RuntimeError(f"futu_sdk_unavailable: {exc}") from exc
            context = OpenQuoteContext(host=self.host, port=self.port)
            try:
                yield context
            finally:
                close = getattr(context, "close", None)
                if callable(close):
                    close()
            return

        from app.clients.futu_pool import get_futu_pool

        with get_futu_pool(host=self.host, port=self.port, start=False).connection() as context:
            yield context

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
        try:
            with self._borrow_context() as context:
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
        with self._borrow_context() as context:
            all_rows: list[dict[str, Any]] = []
            for start_index in range(0, len(code_list), self.snapshot_batch_size):
                batch = code_list[start_index : start_index + self.snapshot_batch_size]
                ret, data = context.get_market_snapshot(batch)
                if ret != RET_OK:
                    raise RuntimeError(str(data))
                all_rows.extend(_as_rows(data))
            return all_rows

    def get_option_chain_rows(
        self,
        futu_code: str,
        *,
        start: str,
        end: str,
        contract_type: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._borrow_context() as context:
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
            "code": ticker,
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


    def get_option_screen_leaderboard(
        self,
        *,
        vol_oi_min: float = 3.0,
        volume_min: int = 500,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Fetch top unusual US stock options via Futu get_option_screen."""
        result = self.get_option_screen_board(
            sort_indicator="VOL_OI_RATIO",
            sort_desc=True,
            option_filters=[
                {"indicator": "VOL_OI_RATIO", "lower": float(vol_oi_min)},
                {"indicator": "VOLUME", "lower": int(volume_min)},
            ],
            limit=limit,
        )
        if result.get("contracts") is None and result.get("items") is not None:
            result["contracts"] = result["items"]
        return result

    def get_option_screen_board(
        self,
        *,
        sort_indicator: str,
        sort_desc: bool = True,
        sort_scope: str = "option",
        option_filters: list[dict[str, Any]] | None = None,
        underlying_filters: list[dict[str, Any]] | None = None,
        underlying_retrieves: tuple[str, ...] | None = None,
        limit: int = 150,
    ) -> dict[str, Any]:
        """Fetch a ranked US options board via Futu get_option_screen."""
        if not self.enabled:
            return {"items": [], "contracts": [], "universe_count": 0, "error": "futu_not_enabled"}

        capped_limit = max(1, min(int(limit), 300))
        started = time.monotonic()
        try:
            with self._borrow_context() as context:
                from futu import OptIndicator, OptMarketCategory, OptUnderlyingIndicator, OptionScreenRequest, RET_OK

                indicator_map = {name: getattr(OptIndicator, name) for name in dir(OptIndicator) if name.isupper()}
                underlying_map = {
                    name: getattr(OptUnderlyingIndicator, name)
                    for name in dir(OptUnderlyingIndicator)
                    if name.isupper()
                }
                scope = sort_scope.strip().lower()
                sort_map = underlying_map if scope == "underlying" else indicator_map
                sort_key = sort_map.get(sort_indicator.upper())
                if sort_key is None:
                    raise ValueError(f"unknown_sort_indicator:{sort_indicator}:{scope}")

                request = OptionScreenRequest(market_categories=[OptMarketCategory.US_STOCK])
                # Populate row["underlying"]["price"] for moneyness + strike sanity checks.
                stock_price_key = underlying_map.get("STOCK_PRICE")
                if stock_price_key is not None:
                    request.add_underlying_retrieve(stock_price_key)

                for retrieve_name in underlying_retrieves or ():
                    retrieve_key = underlying_map.get(str(retrieve_name).upper())
                    if retrieve_key is not None:
                        request.add_underlying_retrieve(retrieve_key)

                for filt in underlying_filters or []:
                    indicator_name = str(filt.get("indicator") or "").upper()
                    indicator = underlying_map.get(indicator_name)
                    if indicator is None:
                        continue
                    values = filt.get("values")
                    lower = filt.get("lower")
                    upper = filt.get("upper")
                    if values:
                        request.add_underlying_filter(indicator, values=list(values))
                    else:
                        request.add_underlying_filter(indicator, lower=lower, upper=upper)

                for filt in option_filters or []:
                    indicator_name = str(filt.get("indicator") or "").upper()
                    indicator = indicator_map.get(indicator_name)
                    if indicator is None:
                        continue
                    values = filt.get("values")
                    lower = filt.get("lower")
                    upper = filt.get("upper")
                    if values:
                        request.add_option_filter(indicator, values=list(values))
                    else:
                        request.add_option_filter(
                            indicator,
                            lower=lower,
                            upper=upper,
                        )

                request.add_sort(sort_key, desc=bool(sort_desc))
                request.page_from = 0
                request.page_count = capped_limit

                ret, payload = context.get_option_screen(request)
                if ret != RET_OK:
                    raise RuntimeError(str(payload))

                _last_page, universe_count, frame = payload
                rows = _as_rows(frame)
                items: list[dict[str, Any]] = []
                for index, row in enumerate(rows, start=1):
                    mapped = self._map_option_screen_row(row, rank=index)
                    if mapped:
                        items.append(mapped)

                elapsed_ms = round((time.monotonic() - started) * 1000.0, 1)
                synced_at = datetime.now(timezone.utc).isoformat()
                return {
                    "items": items,
                    "contracts": items,
                    "universe_count": int(universe_count or 0),
                    "latency_ms": elapsed_ms,
                    "synced_at": synced_at,
                }
        except Exception as exc:
            logger.warning("Futu option screen board failed sort=%s: %s", sort_indicator, exc)
            return {
                "items": [],
                "contracts": [],
                "universe_count": 0,
                "error": f"futu_option_screen_failed: {exc}",
                "synced_at": datetime.now(timezone.utc).isoformat(),
            }

    def _map_option_screen_row(self, row: dict[str, Any], *, rank: int) -> dict[str, Any] | None:
        code = str(row.get("code") or "").strip()
        option_name = str(row.get("option_name") or code).strip()
        if not code and not option_name:
            return None

        option_type_raw = row.get("option_type")
        contract_type = "call" if option_type_raw == 1 else "put" if option_type_raw == 2 else None
        type_letter = "C" if contract_type == "call" else "P" if contract_type == "put" else "?"

        underlying_info = row.get("underlying")
        underlying = None
        spot: float | None = None
        iv_rank_pct: float | None = None
        if isinstance(underlying_info, dict):
            owner_code = underlying_info.get("code") or underlying_info.get("stock_code")
            if owner_code:
                underlying = display_symbol_from_futu_code(str(owner_code))
            spot_raw = underlying_info.get("price")
            if isinstance(spot_raw, (int, float)) and spot_raw > 0:
                spot = float(spot_raw)
            iv_rank_raw = _safe_float(underlying_info.get("iv_rank"))
            if iv_rank_raw is not None:
                iv_rank_pct = round(iv_rank_raw * 100.0, 2) if iv_rank_raw <= 1 else round(iv_rank_raw, 2)
        if not underlying:
            underlying = option_name.split()[0] if option_name else display_symbol_from_futu_code(code)

        strike = _resolve_option_strike(row)
        if strike is None:
            logger.debug("skip option screen row: unresolved strike code=%s name=%s", code, option_name)
            return None

        volume = _safe_int(row.get("volume")) or 0
        oi = _safe_int(row.get("open_interest")) or 0
        vol_oi = _safe_float(row.get("vol_oi_ratio"))
        if vol_oi is None and oi > 0:
            vol_oi = round(volume / oi, 4)

        expiry_raw = _clean_value(row.get("strike_date"))
        expiry = None
        if expiry_raw:
            raw_exp = str(expiry_raw).strip()
            if len(raw_exp) == 8 and raw_exp.isdigit():
                expiry = f"{raw_exp[:4]}-{raw_exp[4:6]}-{raw_exp[6:8]}"
            else:
                expiry = raw_exp[:10]
        left_day = _safe_int(row.get("left_day"))

        iv_raw = _safe_float(row.get("implied_volatility"))
        iv_pct = round(iv_raw, 2) if iv_raw is not None else None
        if iv_pct is not None and iv_pct <= 2:
            iv_pct = round(iv_pct * 100.0, 2)

        hv_raw = _safe_float(row.get("history_volatility"))
        hv_pct = round(hv_raw, 2) if hv_raw is not None else None
        if hv_pct is not None and hv_pct <= 2:
            hv_pct = round(hv_pct * 100.0, 2)

        premium = _safe_float(row.get("premium"))
        price = _safe_float(row.get("price"))
        if premium is None:
            premium = _safe_float(row.get("mid_price")) or price

        change_ratio = _safe_float(row.get("change_ratio"))
        change_pct = (
            round(change_ratio * 100.0, 2)
            if change_ratio is not None and abs(change_ratio) <= 2
            else change_ratio
        )

        in_the_money_raw = row.get("in_the_money")
        in_the_money = bool(in_the_money_raw) if in_the_money_raw is not None else None

        moneyness = _compute_moneyness(strike, spot, contract_type, in_the_money=in_the_money)

        sell_ann = _safe_float(row.get("sell_annualized_return"))
        if sell_ann is not None and sell_ann <= 2:
            sell_ann = round(sell_ann * 100.0, 2)

        sell_prob = _safe_float(row.get("sell_profit_probability"))
        if sell_prob is not None and sell_prob <= 1:
            sell_prob = round(sell_prob * 100.0, 2)

        itm_prob = _safe_float(row.get("itm_probability"))
        if itm_prob is not None and itm_prob <= 1:
            itm_prob = round(itm_prob * 100.0, 2)

        spread = _safe_float(row.get("bid_ask_spread"))
        turnover = _safe_float(row.get("turnover"))
        oi_mcap = _safe_float(row.get("open_interest_market_cap"))
        iv_hv = _safe_float(row.get("iv_hv_ratio"))

        return {
            "rank": rank,
            "code": code,
            "option_name": option_name,
            "underlying": underlying,
            "option_type": type_letter,
            "contract_type": contract_type,
            "strike": strike,
            "strike_price": strike,
            "expiry": expiry,
            "dte": left_day,
            "volume": volume,
            "oi": oi,
            "vol_oi_ratio": round(vol_oi, 4) if vol_oi is not None else None,
            "turnover": turnover,
            "oi_mcap": oi_mcap,
            "premium": premium,
            "price": price,
            "iv": iv_pct,
            "iv_rank": iv_rank_pct,
            "hv": hv_pct,
            "iv_hv": iv_hv,
            "delta": _safe_float(row.get("delta")),
            "gamma": _safe_float(row.get("gamma")),
            "vega": _safe_float(row.get("vega")),
            "theta": _safe_float(row.get("theta")),
            "change_ratio": change_pct,
            "in_the_money": in_the_money,
            "moneyness": moneyness,
            "sell_ann": sell_ann,
            "sell_prob": sell_prob,
            "itm_prob": itm_prob,
            "spread": spread,
            "bid_vol": _safe_int(row.get("bid_volume")),
            "ask_vol": _safe_int(row.get("ask_volume")),
            "underlying_price": spot,
        }


def get_futu_client() -> FutuQuoteClient:
    return FutuQuoteClient.from_settings()
