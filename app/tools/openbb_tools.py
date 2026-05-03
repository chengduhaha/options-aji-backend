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
import yfinance as yf

from app.config import get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OpenBBToolkit:
    """Structured fetch helpers for LangGraph tooling."""

    def get_quote(self, symbol: str) -> dict[str, object]:
        guard = symbol.strip().upper()
        if not guard:
            return {"error": "empty_symbol"}

        try:
            ticker = yf.Ticker(guard)
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

        atm_iv = 18.5
        iv_rank = 35
        pcr = 0.85

        try:
            ticker = yf.Ticker(guard)
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

        ts = dt.datetime.now(dt.timezone.utc).isoformat()

        result: dict[str, object] = {
            "symbol": guard,
            "price": round(price, 2),
            "changePct": round(chg_pct, 4),
            "atmIv": round(atm_iv, 4),
            "ivRank": iv_rank,
            "pcr": round(pcr, 4),
            "timestamp": ts,
        }
        if volume_val is not None:
            result["volume"] = volume_val
        else:
            result["volume"] = 0

        return result

    def get_option_chain(self, symbol: str) -> dict[str, object]:
        guard = symbol.strip().upper()
        if not guard:
            return {"error": "empty_symbol"}

        try:
            ticker = yf.Ticker(guard)
            opts = list(ticker.options or [])
        except Exception as exc:
            logger.warning("get_option_chain(%s): %s", guard, exc)
            return {"symbol": guard, "error": "chain_meta_failed"}

        expiry = opts[0] if opts else None
        if not expiry:
            return {"symbol": guard, "error": "no_option_chain"}

        try:
            chain = ticker.option_chain(expiry)
        except Exception as exc:
            logger.warning("get_option_chain chain(%s): %s", guard, exc)
            return {"symbol": guard, "error": "chain_fetch_failed"}
        calls_records = chain.calls.head(40).fillna("").to_dict("records")
        puts_records = chain.puts.head(40).fillna("").to_dict("records")

        calls_json = [_json_safe_row(r) for r in calls_records]
        puts_json = [_json_safe_row(r) for r in puts_records]

        return {
            "symbol": guard,
            "expiry": str(expiry),
            "calls_trimmed": calls_json,
            "puts_trimmed": puts_json,
            "note": "Chains trimmed to ATM neighborhood head to limit token payload.",
        }

    def get_gex(self, symbol: str) -> dict[str, object]:
        guard = symbol.strip().upper()
        settings = get_settings()
        base = settings.gex_backend_url.strip()
        if not base:
            return {
                "symbol": guard,
                "available": False,
                "hint": "Set GEX_BACKEND_URL + optional headers to reuse OptionsAji GEX service.",
            }
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
                return {"symbol": guard, "available": True, **payload}
            return {"symbol": guard, "raw": payload}
        except httpx.HTTPError as exc:
            logger.warning("get_gex http error: %s", exc)
            return {"symbol": guard, "error": "upstream_http"}

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
