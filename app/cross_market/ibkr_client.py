"""High-level IBKR market data: equities, options (IV/Greeks), chain snapshots, news."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime, timedelta
from typing import TypedDict

from app.cross_market import ibkr_connection as ib_conn

logger = logging.getLogger(__name__)

# IB API tickType / Ticker.marketDataType: 1 live, 2 frozen, 3 delayed, 4 delayed frozen
_MARKET_DATA_TYPE_LABELS: dict[int, str] = {
    1: "live",
    2: "frozen",
    3: "delayed",
    4: "delayed_frozen",
}

# Option generic ticks: volume, OI, implied vol / model greeks stream
_OPTION_GENERIC_TICKS = "100,101,106"


class IBKRQuoteResult(TypedDict, total=False):
    ok: bool
    error: str
    symbol: str
    conId: int
    last: float | None
    bid: float | None
    ask: float | None
    close: float | None
    open: float | None
    high: float | None
    low: float | None
    volume: float | None
    bid_size: float | None
    ask_size: float | None
    market_data_type: int | None
    market_data_type_label: str
    market_data_note: str


class IBKROptionChainResult(TypedDict, total=False):
    ok: bool
    error: str
    symbol: str
    exchange: str
    expirations: list[str]
    strikes: list[float]


class IBKROptionQuoteResult(TypedDict, total=False):
    ok: bool
    error: str
    symbol: str
    expiry: str
    strike: float
    right: str
    conId: int | None
    last: float | None
    bid: float | None
    ask: float | None
    close: float | None
    volume: float | None
    open_interest: float | None
    implied_volatility: float | None
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None
    model_opt_price: float | None
    und_price: float | None
    market_data_type: int | None
    market_data_type_label: str


class IBKRChainRow(TypedDict, total=False):
    strike: float
    right: str
    last: float | None
    bid: float | None
    ask: float | None
    volume: float | None
    open_interest: float | None
    implied_volatility: float | None
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None


class IBKRNewsItem(TypedDict, total=False):
    time: str
    provider_code: str
    article_id: str
    headline: str


def _finite_or_none(value: object) -> float | None:
    if value is None:
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    if x != x:  # NaN
        return None
    return x


def _mdt_label_and_note(ticker: object) -> tuple[int | None, str, str]:
    mdt_raw = getattr(ticker, "marketDataType", None)
    try:
        mdt_int = int(mdt_raw) if mdt_raw is not None else None
    except (TypeError, ValueError):
        mdt_int = None
    label = _MARKET_DATA_TYPE_LABELS.get(mdt_int, "unknown") if mdt_int is not None else "unknown"
    note = (
        "realtime_entitled"
        if mdt_int == 1
        else "delayed_or_non_realtime_typical_without_exchange_subscriptions"
    )
    return mdt_int, label, note


def _greeks_from_ticker(ticker: object) -> dict[str, float | None]:
    mg = None
    for attr in ("modelGreeks", "lastGreeks", "bidGreeks", "askGreeks"):
        mg = getattr(ticker, attr, None)
        if mg is not None:
            break
    if mg is None:
        iv = _finite_or_none(getattr(ticker, "impliedVolatility", None))
        return {
            "implied_volatility": iv,
            "delta": None,
            "gamma": None,
            "theta": None,
            "vega": None,
            "model_opt_price": None,
            "und_price": None,
        }
    return {
        "implied_volatility": _finite_or_none(mg.impliedVol),
        "delta": _finite_or_none(mg.delta),
        "gamma": _finite_or_none(mg.gamma),
        "theta": _finite_or_none(mg.theta),
        "vega": _finite_or_none(mg.vega),
        "model_opt_price": _finite_or_none(mg.optPrice),
        "und_price": _finite_or_none(mg.undPrice),
    }


async def _wait_ticker_ticks(ticker: object, iterations: int = 80) -> None:
    for _ in range(iterations):
        await asyncio.sleep(0.1)
        last = getattr(ticker, "last", None)
        bid = getattr(ticker, "bid", None)
        ask = getattr(ticker, "ask", None)
        if last is not None and str(last) != "nan":
            break
        if bid is not None and ask is not None and str(bid) != "nan" and str(ask) != "nan":
            break


async def get_stock_quote_ibkr(symbol: str) -> IBKRQuoteResult:
    """Equity NBBO / last / session OHLC via reqMktData."""
    sym = symbol.strip().upper()
    if not sym:
        return {"ok": False, "error": "symbol_required"}
    if not ib_conn.ibkr_is_enabled():
        return {"ok": False, "error": "ibkr_disabled"}
    await ib_conn.ibkr_ensure_connected()
    if not ib_conn.ibkr_is_connected():
        return {"ok": False, "error": "ibkr_not_connected"}

    from ib_insync import Stock

    ib = ib_conn.get_ib()
    contract = Stock(sym, "SMART", "USD")
    try:
        qualified = await ib.qualifyContractsAsync(contract)
        if not qualified:
            return {"ok": False, "error": "qualify_contracts_failed", "symbol": sym}
        c = qualified[0]
        ticker = ib.reqMktData(c, "", False, False)
        try:
            await _wait_ticker_ticks(ticker)
            mdt_int, mdt_label, note = _mdt_label_and_note(ticker)
            return {
                "ok": True,
                "symbol": str(c.symbol),
                "conId": int(c.conId),
                "last": _finite_or_none(ticker.last),
                "bid": _finite_or_none(ticker.bid),
                "ask": _finite_or_none(ticker.ask),
                "close": _finite_or_none(ticker.close),
                "open": _finite_or_none(getattr(ticker, "open", None)),
                "high": _finite_or_none(getattr(ticker, "high", None)),
                "low": _finite_or_none(getattr(ticker, "low", None)),
                "volume": _finite_or_none(ticker.volume),
                "bid_size": _finite_or_none(getattr(ticker, "bidSize", None)),
                "ask_size": _finite_or_none(getattr(ticker, "askSize", None)),
                "market_data_type": mdt_int,
                "market_data_type_label": mdt_label,
                "market_data_note": note,
            }
        finally:
            ib.cancelMktData(c)
    except Exception as exc:
        logger.exception("IBKR quote failed symbol=%s", sym)
        return {"ok": False, "error": f"ibkr_quote_failed: {exc}", "symbol": sym}


async def get_option_chain_params_ibkr(symbol: str) -> IBKROptionChainResult:
    """SMART option expirations + strikes for underlying."""
    sym = symbol.strip().upper()
    if not sym:
        return {"ok": False, "error": "symbol_required"}
    if not ib_conn.ibkr_is_enabled():
        return {"ok": False, "error": "ibkr_disabled"}
    await ib_conn.ibkr_ensure_connected()
    if not ib_conn.ibkr_is_connected():
        return {"ok": False, "error": "ibkr_not_connected"}

    from ib_insync import Stock

    ib = ib_conn.get_ib()
    contract = Stock(sym, "SMART", "USD")
    try:
        qualified = await ib.qualifyContractsAsync(contract)
        if not qualified:
            return {"ok": False, "error": "qualify_contracts_failed", "symbol": sym}
        c = qualified[0]
        chains = await ib.reqSecDefOptParamsAsync(c.symbol, "", c.secType, c.conId)
        smart = next((ch for ch in chains if getattr(ch, "exchange", "") == "SMART"), None)
        if smart is None:
            return {"ok": False, "error": "no_smart_option_chain", "symbol": sym}
        exps = sorted(str(e) for e in (smart.expirations or []))
        strikes = sorted(float(s) for s in (smart.strikes or []))
        return {
            "ok": True,
            "symbol": str(c.symbol),
            "exchange": "SMART",
            "expirations": exps,
            "strikes": strikes,
        }
    except Exception as exc:
        logger.exception("IBKR option chain params failed symbol=%s", sym)
        return {"ok": False, "error": f"ibkr_chain_failed: {exc}", "symbol": sym}


async def get_option_quote_ibkr(
    symbol: str,
    expiry_ymd: str,
    strike: float,
    right: str,
) -> IBKROptionQuoteResult:
    """Single option contract: bid/ask/last, volume/OI, model IV & Greeks (generic tick 106)."""
    sym = symbol.strip().upper()
    exp = expiry_ymd.strip().replace("-", "")
    r = right.strip().upper()[:1]
    if not sym or len(exp) != 8 or r not in {"C", "P"}:
        return {"ok": False, "error": "invalid_args", "symbol": sym, "expiry": exp, "strike": strike, "right": r}
    if not ib_conn.ibkr_is_enabled():
        return {"ok": False, "error": "ibkr_disabled", "symbol": sym, "expiry": exp, "strike": strike, "right": r}
    await ib_conn.ibkr_ensure_connected()
    if not ib_conn.ibkr_is_connected():
        return {"ok": False, "error": "ibkr_not_connected", "symbol": sym, "expiry": exp, "strike": strike, "right": r}

    from ib_insync import Option

    ib = ib_conn.get_ib()
    opt = Option(sym, exp, strike, r, "SMART", currency="USD")
    try:
        qualified = await ib.qualifyContractsAsync(opt)
        if not qualified:
            return {
                "ok": False,
                "error": "qualify_option_failed",
                "symbol": sym,
                "expiry": exp,
                "strike": strike,
                "right": r,
            }
        c = qualified[0]
        ticker = ib.reqMktData(c, _OPTION_GENERIC_TICKS, False, False)
        try:
            await _wait_ticker_ticks(ticker, 100)
            gx = _greeks_from_ticker(ticker)
            mdt_int, mdt_label, _note = _mdt_label_and_note(ticker)
            return {
                "ok": True,
                "symbol": sym,
                "expiry": exp,
                "strike": float(strike),
                "right": r,
                "conId": int(c.conId),
                "last": _finite_or_none(ticker.last),
                "bid": _finite_or_none(ticker.bid),
                "ask": _finite_or_none(ticker.ask),
                "close": _finite_or_none(ticker.close),
                "volume": _finite_or_none(ticker.volume),
                "open_interest": _finite_or_none(
                    ticker.callOpenInterest if r == "C" else ticker.putOpenInterest
                ),
                "implied_volatility": gx["implied_volatility"],
                "delta": gx["delta"],
                "gamma": gx["gamma"],
                "theta": gx["theta"],
                "vega": gx["vega"],
                "model_opt_price": gx["model_opt_price"],
                "und_price": gx["und_price"],
                "market_data_type": mdt_int,
                "market_data_type_label": mdt_label,
            }
        finally:
            ib.cancelMktData(c)
    except Exception as exc:
        logger.exception("IBKR option quote failed %s %s %s %s", sym, exp, strike, r)
        return {
            "ok": False,
            "error": f"ibkr_option_quote_failed: {exc}",
            "symbol": sym,
            "expiry": exp,
            "strike": float(strike),
            "right": r,
        }


def _pick_nearest_expiry(expirations: list[str], *, yyyymmdd: str | None) -> str | None:
    if yyyymmdd:
        y = yyyymmdd.replace("-", "")
        if y in expirations:
            return y
        return None
    today = date.today().strftime("%Y%m%d")
    future = [e for e in expirations if e >= today]
    return min(future, key=lambda e: e) if future else (expirations[0] if expirations else None)


def _strikes_window(strikes: list[float], spot: float, each_side: int) -> list[float]:
    if not strikes or spot <= 0:
        return []
    ordered = sorted(strikes, key=lambda s: abs(s - spot))
    picked: list[float] = []
    for s in ordered:
        if s not in picked:
            picked.append(s)
        if len(picked) >= 2 * each_side + 1:
            break
    return sorted(picked)


async def get_option_chain_snapshot_ibkr(
    symbol: str,
    expiry: str | None = None,
    strikes_each_side: int = 4,
) -> dict:
    """ATM-focused call+put quotes with IV/Greeks; bounded requests for IB pacing."""
    sym = symbol.strip().upper()
    if not sym:
        return {"ok": False, "error": "symbol_required"}
    if not ib_conn.ibkr_is_enabled():
        return {"ok": False, "error": "ibkr_disabled"}
    await ib_conn.ibkr_ensure_connected()
    if not ib_conn.ibkr_is_connected():
        return {"ok": False, "error": "ibkr_not_connected"}

    cap = max(1, min(int(strikes_each_side), 12))
    chain = await get_option_chain_params_ibkr(sym)
    if not chain.get("ok"):
        return {"ok": False, "error": chain.get("error", "chain_failed"), "symbol": sym}

    exps: list[str] = chain["expirations"]
    strikes_all: list[float] = chain["strikes"]
    exp_use = _pick_nearest_expiry(exps, yyyymmdd=expiry.strip().replace("-", "") if expiry else None)
    if not exp_use:
        return {"ok": False, "error": "no_expiry_matched", "symbol": sym}

    uq = await get_stock_quote_ibkr(sym)
    spot = 0.0
    if uq.get("ok"):
        spot = float(uq.get("last") or 0) or float(
            ((uq.get("bid") or 0) + (uq.get("ask") or 0)) / 2
            if uq.get("bid") and uq.get("ask")
            else 0
        )
    win = _strikes_window(strikes_all, spot, cap)
    if not win:
        win = strikes_all[: 2 * cap + 1] if strikes_all else []

    rows: list[IBKRChainRow] = []
    for k in win:
        for right in ("C", "P"):
            row = await get_option_quote_ibkr(sym, exp_use, float(k), right)
            if row.get("ok"):
                rows.append(
                    {
                        "strike": float(k),
                        "right": right,
                        "last": row.get("last"),
                        "bid": row.get("bid"),
                        "ask": row.get("ask"),
                        "volume": row.get("volume"),
                        "open_interest": row.get("open_interest"),
                        "implied_volatility": row.get("implied_volatility"),
                        "delta": row.get("delta"),
                        "gamma": row.get("gamma"),
                        "theta": row.get("theta"),
                        "vega": row.get("vega"),
                    }
                )

    return {
        "ok": True,
        "symbol": sym,
        "expiry": exp_use,
        "underlying_hint_price": spot,
        "strikes_included": win,
        "contracts": rows,
        "source": "ibkr",
    }


async def get_options_landscape_ibkr(ticker: str, expiry_within_days: int = 30) -> dict:
    """Summary similar to Massive get_options_landscape for agent compatibility."""
    sym = ticker.strip().upper()
    if not sym:
        return {"error": "ticker is required"}
    if expiry_within_days <= 0:
        return {"error": "expiry_within_days must be > 0"}

    chain = await get_option_chain_params_ibkr(sym)
    if not chain.get("ok"):
        return {"error": chain.get("error", "no_chain"), "ticker": sym}

    exps: list[str] = chain["expirations"]
    today = date.today()
    horizon = today + timedelta(days=expiry_within_days)

    def _exp_dt(e: str) -> date | None:
        if len(e) != 8:
            return None
        try:
            return date(int(e[:4]), int(e[4:6]), int(e[6:8]))
        except ValueError:
            return None

    exps_in = [e for e in exps if (d := _exp_dt(e)) is not None and d <= horizon]
    if not exps_in:
        exps_in = exps[:3]

    uq = await get_stock_quote_ibkr(sym)
    spot = 0.0
    if uq.get("ok"):
        spot = float(uq.get("last") or 0) or float(
            ((uq.get("bid") or 0) + (uq.get("ask") or 0)) / 2
            if uq.get("bid") and uq.get("ask")
            else 0
        )
    if spot <= 0:
        return {"error": f"Invalid underlying price for {sym}", "ticker": sym}

    exp_use = min(exps_in, key=lambda e: _exp_dt(e) or date.max)

    strikes_all = chain["strikes"]
    atm_band = [s for s in strikes_all if abs(s - spot) / spot < 0.05]
    ivs: list[float] = []
    for s in atm_band[:6]:
        for r in ("C", "P"):
            q = await get_option_quote_ibkr(sym, exp_use, float(s), r)
            if q.get("ok") and q.get("implied_volatility") is not None:
                ivs.append(float(q["implied_volatility"]))

    avg_iv = sum(ivs) / len(ivs) if ivs else 0.0
    approx_contracts = max(len(strikes_all) * len(exps_in) * 2, 0)

    return {
        "ticker": sym,
        "expiry_within_days": expiry_within_days,
        "underlying_price": round(spot, 4),
        "atm_iv": round(avg_iv, 4),
        "total_contracts": approx_contracts,
        "atm_contract_count": len(atm_band),
        "nearest_expiry_used": exp_use,
        "source": "ibkr",
    }


async def aggregate_option_volumes_cp_ratio_ibkr(ticker: str, strikes_each_side: int = 6) -> tuple[float, float, float]:
    """(call_vol, put_vol, call_share) for infer_options_probability."""
    snap = await get_option_chain_snapshot_ibkr(ticker, expiry=None, strikes_each_side=strikes_each_side)
    if not snap.get("ok"):
        return 0.0, 0.0, 0.5
    cv = 0.0
    pv = 0.0
    for row in snap.get("contracts", []):
        v = float(row.get("volume") or 0)
        if row.get("right") == "C":
            cv += v
        elif row.get("right") == "P":
            pv += v
    tot = cv + pv
    share = (cv / tot) if tot > 0 else 0.5
    return cv, pv, share


async def get_ibkr_news_headlines(symbol: str, limit: int = 15) -> dict:
    """Historical news headlines for contract (requires IB news permissions / subscriptions)."""
    sym = symbol.strip().upper()
    if not sym:
        return {"ok": False, "error": "symbol_required", "items": []}
    if not ib_conn.ibkr_is_enabled():
        return {"ok": False, "error": "ibkr_disabled", "items": []}
    await ib_conn.ibkr_ensure_connected()
    if not ib_conn.ibkr_is_connected():
        return {"ok": False, "error": "ibkr_not_connected", "items": []}

    from ib_insync import Stock

    lim = max(1, min(int(limit), 300))
    providers = os.getenv("IBKR_NEWS_PROVIDERS", "BRFG+BZ+FLY").strip() or "BRFG+BZ+FLY"
    ib = ib_conn.get_ib()
    contract = Stock(sym, "SMART", "USD")
    try:
        qualified = await ib.qualifyContractsAsync(contract)
        if not qualified:
            return {"ok": False, "error": "qualify_contracts_failed", "symbol": sym, "items": []}
        c = qualified[0]
        con_id = int(c.conId)
        end = date.today()
        start = end - timedelta(days=14)
        raw = await ib.reqHistoricalNewsAsync(con_id, providers, start, end, lim, [])
        items: list[IBKRNewsItem] = []
        if raw is None:
            return {"ok": True, "symbol": sym, "items": [], "note": "no_news_or_timeout"}
        if not isinstance(raw, list):
            raw = [raw]
        for article in raw:
            t = getattr(article, "time", None)
            items.append(
                {
                    "time": t.isoformat() if isinstance(t, datetime) else str(t),
                    "provider_code": str(getattr(article, "providerCode", "") or ""),
                    "article_id": str(getattr(article, "articleId", "") or ""),
                    "headline": str(getattr(article, "headline", "") or ""),
                }
            )
        return {"ok": True, "symbol": sym, "items": items, "provider_codes": providers}
    except Exception as exc:
        logger.exception("IBKR news failed symbol=%s", sym)
        return {"ok": False, "error": f"ibkr_news_failed: {exc}", "symbol": sym, "items": []}
