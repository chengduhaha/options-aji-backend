"""情景对照类 Copilot tools."""
from __future__ import annotations

from langchain_core.tools import tool


@tool
async def recommend_options_strike(
    ticker: str,
    direction: str,
    underlying_price: float,
    confidence: float = 0.5,
) -> dict:
    """根据方向和置信度参考行权价和 DTE."""
    if not ticker.strip():
        return {"error": "ticker is required"}
    if direction not in {"bullish", "bearish"}:
        return {"error": "direction must be bullish or bearish"}
    if underlying_price <= 0:
        return {"error": "underlying_price must be > 0"}
    if confidence < 0 or confidence > 1:
        return {"error": "confidence must be in [0, 1]"}

    strike_offset_pct = 0.02 if confidence > 0.7 else 0.05
    if direction == "bullish":
        recommended_strike = round(underlying_price * (1 + strike_offset_pct), 0)
        option_type = "CALL"
    else:
        recommended_strike = round(underlying_price * (1 - strike_offset_pct), 0)
        option_type = "PUT"

    dte = 7 if confidence > 0.7 else 21
    return {
        "ticker": ticker.upper(),
        "option_type": option_type,
        "recommended_strike": recommended_strike,
        "days_to_expiry": dte,
        "rationale_zh": (
            f"基于 {direction} 情景(置信度 {confidence:.0%}),"
            f" 参考 {dte} 天到期的 {recommended_strike} {option_type}"
        ),
    }


@tool
async def design_multi_asset_portfolio(
    affected_tickers: list[str],
    event_probability: float,
    max_capital_usd: float = 5000,
) -> dict:
    """根据事件影响范围和概率设计多资产组合."""
    if not affected_tickers:
        return {"error": "affected_tickers is required"}
    if event_probability < 0 or event_probability > 1:
        return {"error": "event_probability must be in [0, 1]"}
    if max_capital_usd <= 0:
        return {"error": "max_capital_usd must be > 0"}

    size = min(len(affected_tickers), 5)
    per_position = max_capital_usd / size
    instrument = "options_put_spread" if event_probability > 0.5 else "options_call_spread"

    positions: list[dict] = []
    for ticker in affected_tickers[:size]:
        positions.append(
            {
                "ticker": ticker,
                "allocation_usd": round(per_position, 2),
                "instrument": instrument,
                "rationale_zh": f"基于事件概率 {event_probability:.0%}, 分配 ${per_position:.0f}",
            }
        )

    return {
        "total_capital": max_capital_usd,
        "positions": positions,
        "expected_ev_pct": round(event_probability * 15, 1),
    }
