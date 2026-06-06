"""Market data accessors.

OpenBB Platform SDK pulls many heavy deps and often needs Hub keys. Hermes docs
prefer yfinance for option chains alongside optional upstream GEX REST.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
from dataclasses import dataclass
from typing import Optional, cast

import httpx

from app.analytics.gex_compute import compute_gex_profile
from app.analytics.iv_metrics import hv_series_and_current, iv_rank_percentile_proxy
from app.clients.fmp_client import get_fmp_client
from app.clients.futu_client import get_futu_client
from app.config import get_settings
from app.tools.yf_helpers import yf_ticker

logger = logging.getLogger(__name__)


def _quote_dict_from_fmp_row(guard: str, d: dict[str, object]) -> dict[str, object] | None:
    """Map FMP /stable/quote fields to get_quote() shape."""
    price_raw = d.get("price")
    if price_raw is None:
        return None
    try:
        last = float(price_raw)
    except (TypeError, ValueError):
        return None
    if math.isnan(last):
        return None

    prev_raw = d.get("previousClose")
    prev: float | None = None
    if prev_raw is not None and str(prev_raw) != "":
        try:
            prev = float(prev_raw)
        except (TypeError, ValueError):
            prev = None
    if prev is None:
        chg = d.get("change")
        if isinstance(chg, (int, float)) and not (isinstance(chg, float) and math.isnan(chg)):
            prev = last - float(chg)

    pct_raw = d.get("changesPercentage")
    if pct_raw is None:
        pct_raw = d.get("changePercentage")
    pct: float | None = None
    if pct_raw is not None and str(pct_raw) != "":
        try:
            pct = float(pct_raw)
        except (TypeError, ValueError):
            pct = None
    if pct is None and prev is not None and prev > 0:
        pct = (last - prev) / prev * 100.0

    day_high = d.get("dayHigh")
    day_low = d.get("dayLow")
    vol = d.get("volume")
    open_ = d.get("open")
    return {
        "symbol": guard,
        "last_price": last,
        "previous_close": prev,
        "change_pct": pct,
        "regular_market_open": open_,
        "day_high": day_high,
        "day_low": day_low,
        "volume": vol,
    }


@dataclass(frozen=True)
class OpenBBToolkit:
    """Structured fetch helpers for LangGraph tooling."""

    def get_quote(self, symbol: str) -> dict[str, object]:
        guard = symbol.strip().upper()
        if not guard:
            return {"error": "empty_symbol"}

        settings = get_settings()
        futu_on = getattr(settings, "futu_enabled", False)
        if futu_on:
            try:
                row = get_futu_client().get_stock_quote(guard)
                if isinstance(row, dict) and not row.get("error"):
                    return row
            except Exception as exc:
                logger.warning("get_quote Futu(%s): %s", guard, exc)
            logger.warning("get_quote Futu(%s) unavailable, falling back to FMP/yfinance", guard)

        if settings.fmp_api_key.strip():
            try:
                fmp = get_fmp_client()
                row = fmp.get_quote(guard)
                if isinstance(row, dict):
                    parsed = _quote_dict_from_fmp_row(guard, row)
                    if parsed is not None:
                        return parsed
                if guard.startswith("^"):
                    row2 = fmp.get_index_quote(guard)
                    if isinstance(row2, dict):
                        parsed2 = _quote_dict_from_fmp_row(guard, row2)
                        if parsed2 is not None:
                            return parsed2
            except Exception as exc:
                logger.warning("get_quote FMP(%s): %s", guard, exc)

        try:
            ticker = yf_ticker(guard)
            qi = ticker.fast_info
            last = qi.get("last_price")
            prev = qi.get("previous_close")
        except Exception as exc:
            logger.warning("get_quote(%s): %s", guard, exc)
            return {"symbol": guard, "error": "quote_fetch_failed"}
        pct: Optional[float] = None
        if isinstance(last, (int, float)) and isinstance(prev, (int, float)) and prev:
            pct = float((last - prev) / prev * 100.0)

        return {
            "symbol": guard,
            "last_price": last,
            "previous_close": prev,
            "change_pct": pct,
            "regular_market_open": qi.get("open"),
            "day_high": qi.get("day_high"),
            "day_low": qi.get("day_low"),
            "volume": qi.get("last_volume"),
        }

    def frontend_market_bar(self, symbol: str) -> dict[str, object]:
        """Shape matches OptionsAji `RightPanel` (`price`, camelCase KPIs)."""

        guard = symbol.strip().upper()
        if not guard:
            return {"symbol": guard, "error": "empty_symbol"}

        qt = self.get_quote(guard)
        if qt.get("error"):
            return {"symbol": guard, "error": qt.get("error"), "upstream": qt}

        price_v = qt.get("last_price")
        price = (
            float(price_v)
            if isinstance(price_v, (int, float)) and not (isinstance(price_v, float) and math.isnan(price_v))
            else 0.0
        )
        pct_v = qt.get("change_pct")
        chg_pct = (
            float(pct_v)
            if isinstance(pct_v, (int, float)) and not (isinstance(pct_v, float) and math.isnan(pct_v))
            else 0.0
        )

        volume_raw = qt.get("volume")
        volume_val = _scalar_int(volume_raw)

        # UI fallbacks when chain/PCR cannot be read (real values filled below when Yahoo returns data).
        atm_iv = 18.5
        iv_rank = 35
        pcr = 0.85

        settings = get_settings()
        try:
            if getattr(settings, "futu_enabled", False):
                payload = get_futu_client().get_option_chain_snapshot(guard, limit=2000)
                contracts = list(payload.get("contracts") or []) if isinstance(payload, dict) else []
                atm_iv, pcr = _atm_iv_pcr_from_futu_contracts(contracts, price)
            else:
                ticker = yf_ticker(guard)
                opts_list = list(ticker.options or [])
                expiry = opts_list[0] if opts_list else None
                if expiry is not None and price > 0:
                    oc = ticker.option_chain(expiry)
                    calls_df = oc.calls
                    puts_df = oc.puts
                    if not calls_df.empty and "strike" in calls_df.columns:
                        row_idx = (calls_df["strike"].astype(float) - price).abs().idxmin()
                        atm_row = calls_df.loc[row_idx]
                        iv_raw = atm_row.get("impliedVolatility")
                        if iv_raw is not None:
                            iv_f = float(iv_raw)
                            if not math.isnan(iv_f) and iv_f > 0:
                                atm_iv = iv_f * 100.0
                    cv = calls_df["volume"].fillna(0).astype(float).sum() if not calls_df.empty else 0.0
                    pv = puts_df["volume"].fillna(0).astype(float).sum() if not puts_df.empty else 0.0
                    if cv > 0 and pv >= 0:
                        pcr = float(pv / cv)
                    elif pv > 0 and cv <= 0:
                        pcr = 9.99
        except Exception as exc:
            logger.warning("frontend_market_bar extras(%s): %s", guard, exc)

        iv_pctile: Optional[float] = None
        iv_note = "iv_rank_placeholder"
        try:
            hv_series, _hv_meta = hv_series_and_current(guard)
            hv_vals = [v for _, v in hv_series]
            rank_est, pct_est, iv_note = iv_rank_percentile_proxy(
                current_iv_pct=float(atm_iv),
                hv_series_pct=hv_vals,
            )
            if rank_est is not None:
                iv_rank = int(round(rank_est))
            iv_pctile = pct_est
        except Exception as exc:
            logger.warning("frontend_market_bar iv_rank(%s): %s", guard, exc)

        ts = dt.datetime.now(dt.timezone.utc).isoformat()

        result: dict[str, object] = {
            "symbol": guard,
            "price": round(price, 2),
            "changePct": round(chg_pct, 4),
            "atmIv": round(atm_iv, 4),
            "ivRank": iv_rank,
            "ivPercentile": iv_pctile,
            "ivMethodology": iv_note,
            "pcr": round(pcr, 4),
            "timestamp": ts,
        }
        if volume_val is not None:
            result["volume"] = volume_val
        else:
            result["volume"] = 0

        return result

    def get_option_chain(self, symbol: str, *, expiration: Optional[str] = None, head: int = 40) -> dict[str, object]:
        guard = symbol.strip().upper()
        if not guard:
            return {"error": "empty_symbol"}

        settings = get_settings()
        if getattr(settings, "futu_enabled", False):
            try:
                payload = get_futu_client().get_option_chain_snapshot(
                    guard,
                    expiration_date=expiration,
                    limit=max(head * 4, 200),
                )
                if isinstance(payload, dict) and not payload.get("error") and payload.get("contracts"):
                    chain = _futu_payload_to_chain(guard, payload, expiration=expiration, prefetch_expirations=0)
                    calls = list(chain.get("calls") or [])
                    puts = list(chain.get("puts") or [])
                    return {
                        "symbol": guard,
                        "source": "futu",
                        "expiry": chain.get("expiration"),
                        "expirations": chain.get("expirations") or [],
                        "calls_trimmed": calls if head <= 0 else calls[:head],
                        "puts_trimmed": puts if head <= 0 else puts[:head],
                        "note": "Futu OpenAPI real-time option snapshot.",
                    }
            except Exception as exc:
                logger.warning("get_option_chain Futu(%s): %s", guard, exc)

        try:
            ticker = yf_ticker(guard)
            opts = list(ticker.options or [])
        except Exception as exc:
            logger.warning("get_option_chain(%s): %s", guard, exc)
            return {"symbol": guard, "error": "chain_meta_failed"}

        expiry = expiration or (opts[0] if opts else None)
        if not expiry:
            return {"symbol": guard, "error": "no_option_chain"}

        try:
            chain = ticker.option_chain(expiry)
        except Exception as exc:
            logger.warning("get_option_chain chain(%s): %s", guard, exc)
            return {"symbol": guard, "error": "chain_fetch_failed"}
        calls_df = chain.calls
        puts_df = chain.puts
        calls_view = calls_df if head <= 0 else calls_df.head(head)
        puts_view = puts_df if head <= 0 else puts_df.head(head)
        calls_records = calls_view.fillna("").to_dict("records")
        puts_records = puts_view.fillna("").to_dict("records")

        calls_json = [_json_safe_row(cast(dict[str, object], r)) for r in calls_records]
        puts_json = [_json_safe_row(cast(dict[str, object], r)) for r in puts_records]

        return {
            "symbol": guard,
            "expiry": str(expiry),
            "expirations": [str(x) for x in opts],
            "calls_trimmed": calls_json,
            "puts_trimmed": puts_json,
            "note": "Agent digest may use trimmed head; UI should call /api/stock/{sym}/chain?full=1.",
        }

    def get_option_chain_full(
        self,
        symbol: str,
        *,
        expiration: Optional[str] = None,
        prefetch_expirations: int = 0,
    ) -> dict[str, object]:
        guard = symbol.strip().upper()
        if not guard:
            return {"error": "empty_symbol"}
        if expiration is not None:
            prefetch_expirations = 0
        if prefetch_expirations < 0:
            prefetch_expirations = 0

        settings = get_settings()
        if getattr(settings, "futu_enabled", False):
            try:
                payload = get_futu_client().get_option_chain_snapshot(
                    guard,
                    expiration_date=expiration,
                    limit=1500,
                )
                if isinstance(payload, dict) and not payload.get("error") and payload.get("contracts"):
                    return _futu_payload_to_chain(
                        guard,
                        payload,
                        expiration=expiration,
                        prefetch_expirations=prefetch_expirations,
                    )
            except Exception as exc:
                logger.warning("get_option_chain_full Futu(%s): %s", guard, exc)
            if getattr(settings, "futu_enabled", False):
                return {"symbol": guard, "error": "futu_chain_failed"}

        try:
            ticker = yf_ticker(guard)
            opts = list(ticker.options or [])
        except Exception as exc:
            logger.warning("get_option_chain_full(%s): %s", guard, exc)
            return {"symbol": guard, "error": "chain_meta_failed"}

        expiry = expiration or (opts[0] if opts else None)
        if not expiry:
            return {"symbol": guard, "error": "no_option_chain"}

        spot = 0.0
        try:
            lp = ticker.fast_info.get("last_price")
            if isinstance(lp, (int, float)) and not (isinstance(lp, float) and math.isnan(lp)):
                spot = float(lp)
        except Exception:
            pass

        def _rows_for_exp(exp: object) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
            chain = ticker.option_chain(exp)
            calls_records = chain.calls.fillna("").to_dict("records")
            puts_records = chain.puts.fillna("").to_dict("records")
            calls_json = [_json_safe_row(cast(dict[str, object], r)) for r in calls_records]
            puts_json = [_json_safe_row(cast(dict[str, object], r)) for r in puts_records]
            return calls_json, puts_json

        if prefetch_expirations > 0:
            targets = opts[:prefetch_expirations]
            prefetched: list[dict[str, object]] = []
            for exp in targets:
                exp_str = str(exp)
                try:
                    cj, pj = _rows_for_exp(exp)
                    prefetched.append({"expiration": exp_str, "calls": cj, "puts": pj})
                except Exception as exc:
                    logger.warning("get_option_chain_full chain(%s %s): %s", guard, exp_str, exc)
                    continue
            if not prefetched:
                return {"symbol": guard, "error": "chain_fetch_failed"}
            first = prefetched[0]
            return {
                "symbol": guard,
                "expiration": str(first["expiration"]),
                "expirations": [str(x) for x in opts],
                "underlyingPrice": round(spot, 4) if spot > 0 else None,
                "calls": first["calls"],
                "puts": first["puts"],
                "prefetchedChains": prefetched,
            }

        try:
            calls_json, puts_json = _rows_for_exp(expiry)
        except Exception as exc:
            logger.warning("get_option_chain_full chain(%s): %s", guard, exc)
            return {"symbol": guard, "error": "chain_fetch_failed"}

        return {
            "symbol": guard,
            "expiration": str(expiry),
            "expirations": [str(x) for x in opts],
            "underlyingPrice": round(spot, 4) if spot > 0 else None,
            "calls": calls_json,
            "puts": puts_json,
        }

    def get_gex(self, symbol: str) -> dict[str, object]:
        guard = symbol.strip().upper()
        settings = get_settings()
        base = settings.gex_backend_url.strip()
        if base:
            url = base.rstrip("/") + f"/gex/{guard}"
            hdrs_raw = settings.gex_backend_headers.strip()
            headers: dict[str, str] = {}
            if hdrs_raw:
                try:
                    loaded = cast(dict[str, object], json.loads(hdrs_raw))
                    for k, v in loaded.items():
                        headers[str(k)] = str(v)
                except json.JSONDecodeError:
                    logger.warning("gex_backend_headers invalid JSON")

            try:
                with httpx.Client(timeout=20.0) as client:
                    resp = client.get(url, headers=headers)
                    resp.raise_for_status()
                    payload = resp.json()
                if isinstance(payload, dict):
                    merged = dict(payload)
                    merged.setdefault("symbol", guard)
                    return merged
                return {"symbol": guard, "raw": payload, "source": "gex_upstream"}
            except httpx.HTTPError as exc:
                logger.warning("get_gex upstream failed, falling back to local: %s", exc)

        local = compute_gex_profile(guard)
        if local.get("error"):
            return {
                "symbol": guard,
                "available": False,
                "error": local.get("error"),
                "hint": "Upstream GEX unavailable and local chain failed.",
            }
        return local

    def snapshot_bundle(self, symbol: str) -> str:
        merged = {
            "quote": self.get_quote(symbol),
            "option_chain_digest": self.get_option_chain(symbol),
            "gex": self.get_gex(symbol),
        }
        dumped = json.dumps(merged, default=str)
        if len(dumped) > 22_000:
            merged_mini: dict[str, object] = {
                "quote": merged["quote"],
                "gex": merged["gex"],
                "truncated": True,
            }
            return json.dumps(merged_mini, default=str)
        return dumped


def build_default_toolkit() -> OpenBBToolkit:
    return OpenBBToolkit()


def _atm_iv_pcr_from_futu_contracts(
    contracts: list[dict[str, object]],
    spot: float,
) -> tuple[float, float]:
    """Nearest-expiry ATM IV (percent) and put/call volume ratio from Futu contracts."""
    atm_iv = 18.5
    pcr = 0.85
    if spot <= 0 or not contracts:
        return atm_iv, pcr

    by_exp: dict[str, list[dict[str, object]]] = {}
    for rec in contracts:
        if not isinstance(rec, dict):
            continue
        exp = str(rec.get("expiration_date") or "")[:10]
        if exp:
            by_exp.setdefault(exp, []).append(rec)
    if not by_exp:
        return atm_iv, pcr

    nearest = sorted(by_exp.keys())[0]
    slice_rows = by_exp[nearest]
    call_vol = put_vol = 0.0
    best_iv: Optional[float] = None
    best_dist = float("inf")
    for rec in slice_rows:
        side = str(rec.get("contract_type") or "").lower()
        vol = float(rec.get("day_volume") or 0)
        if side == "call":
            call_vol += vol
        elif side == "put":
            put_vol += vol
        strike = rec.get("strike_price")
        iv = rec.get("implied_volatility")
        if not isinstance(strike, (int, float)) or not isinstance(iv, (int, float)):
            continue
        iv_f = float(iv)
        if iv_f <= 0:
            continue
        iv_pct = iv_f * 100.0 if iv_f <= 2.5 else iv_f
        dist = abs(float(strike) - spot)
        if dist < best_dist:
            best_dist = dist
            best_iv = iv_pct
    if best_iv is not None:
        atm_iv = best_iv
    if call_vol > 0:
        pcr = put_vol / call_vol
    elif put_vol > 0:
        pcr = 9.99
    return atm_iv, pcr


def _scalar_int(value: object) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if not math.isnan(value) else None
    if hasattr(value, "item"):
        try:
            return _scalar_int(value.item())
        except Exception:
            return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _json_safe_row(record: dict[str, object]) -> dict[str, object]:
    out: dict[str, object] = {}
    for k, v in record.items():
        if hasattr(v, "item"):
            try:
                out[str(k)] = v.item()
                continue
            except Exception:
                pass
        out[str(k)] = v
    return out


def _futu_contract_to_option_row(contract: dict[str, object]) -> dict[str, object]:
    return {
        "contractSymbol": contract.get("ticker"),
        "strike": contract.get("strike_price"),
        "lastPrice": contract.get("last_trade_price") or contract.get("midpoint"),
        "bid": contract.get("bid"),
        "ask": contract.get("ask"),
        "volume": contract.get("day_volume"),
        "openInterest": contract.get("open_interest"),
        "impliedVolatility": contract.get("implied_volatility"),
        "delta": contract.get("delta"),
        "gamma": contract.get("gamma"),
        "theta": contract.get("theta"),
        "vega": contract.get("vega"),
        "expiration": contract.get("expiration_date"),
    }


def _futu_payload_to_chain(
    guard: str,
    payload: dict[str, object],
    *,
    expiration: Optional[str],
    prefetch_expirations: int,
) -> dict[str, object]:
    contracts = [c for c in list(payload.get("contracts") or []) if isinstance(c, dict)]
    expirations = sorted({str(c.get("expiration_date")) for c in contracts if c.get("expiration_date")})
    selected_expiration = expiration or (expirations[0] if expirations else None)
    selected = [
        c
        for c in contracts
        if selected_expiration is None or str(c.get("expiration_date")) == str(selected_expiration)
    ]
    calls = [
        _futu_contract_to_option_row(c)
        for c in selected
        if str(c.get("contract_type") or "").lower() == "call"
    ]
    puts = [
        _futu_contract_to_option_row(c)
        for c in selected
        if str(c.get("contract_type") or "").lower() == "put"
    ]
    calls.sort(key=lambda row: float(row.get("strike") or 0))
    puts.sort(key=lambda row: float(row.get("strike") or 0))

    result: dict[str, object] = {
        "symbol": guard,
        "source": "futu",
        "expiration": selected_expiration,
        "expirations": expirations,
        "underlyingPrice": None,
        "calls": calls,
        "puts": puts,
    }
    if prefetch_expirations > 0:
        prefetched: list[dict[str, object]] = []
        for exp in expirations[:prefetch_expirations]:
            exp_contracts = [c for c in contracts if str(c.get("expiration_date")) == exp]
            exp_calls = [
                _futu_contract_to_option_row(c)
                for c in exp_contracts
                if str(c.get("contract_type") or "").lower() == "call"
            ]
            exp_puts = [
                _futu_contract_to_option_row(c)
                for c in exp_contracts
                if str(c.get("contract_type") or "").lower() == "put"
            ]
            exp_calls.sort(key=lambda row: float(row.get("strike") or 0))
            exp_puts.sort(key=lambda row: float(row.get("strike") or 0))
            prefetched.append({"expiration": exp, "calls": exp_calls, "puts": exp_puts})
        result["prefetchedChains"] = prefetched
    return result
