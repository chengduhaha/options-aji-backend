"""Data tools exposed to Copilot agent."""
from __future__ import annotations

import asyncio

from langchain_core.tools import tool

from app.clients.fmp_client import get_fmp_client
from app.cross_market import ibkr_connection as ibkr_conn
from app.cross_market.ibkr_client import (
    get_ibkr_news_headlines,
    get_option_chain_params_ibkr as ibkr_fetch_option_chain_params,
    get_option_chain_snapshot_ibkr,
    get_option_quote_ibkr,
    get_options_landscape_ibkr,
    get_stock_quote_ibkr,
)
from app.cross_market.massive_utils import get_options_chain_results
from app.cross_market.polymarket_client import PolymarketClient
from app.cross_market.xpoz_client import XpozClient


@tool
async def polymarket_search_and_quote(query: str, min_liquidity_usd: float = 10000) -> dict:
    """搜索 Polymarket 上与 query 相关的市场."""
    if not query.strip():
        return {"error": "query is required", "markets": [], "count": 0}

    client = PolymarketClient()
    try:
        markets = await client.search_markets(query=query, limit=10)
        filtered: list[dict] = []
        for market in markets:
            liquidity = float(market.get("liquidity") or 0)
            if liquidity < min_liquidity_usd:
                continue
            filtered.append(
                {
                    "market_id": market.get("id"),
                    "slug": market.get("slug"),
                    "question": market.get("question"),
                    "yes_price": float(market.get("lastTradePrice") or 0),
                    "volume_24h": float(market.get("volume24hr") or 0),
                    "liquidity_usd": liquidity,
                    "end_date": market.get("endDate"),
                }
            )
        return {"markets": filtered, "count": len(filtered)}
    except Exception as exc:
        return {"error": f"polymarket_search_failed: {exc}", "markets": [], "count": 0}
    finally:
        await client.close()


@tool
async def get_options_landscape(ticker: str, expiry_within_days: int = 30) -> dict:
    """获取标的期权链全景（优先 IBKR；否则 Massive）。"""
    if not ticker.strip():
        return {"error": "ticker is required"}
    if expiry_within_days <= 0:
        return {"error": "expiry_within_days must be > 0"}

    t = ticker.strip().upper()
    if ibkr_conn.ibkr_is_enabled():
        await ibkr_conn.ibkr_ensure_connected()
        if ibkr_conn.ibkr_is_connected():
            landscape = await get_options_landscape_ibkr(t, expiry_within_days)
            if landscape.get("source") == "ibkr":
                return landscape

    chain = await get_options_chain_results(t)
    results = chain.get("results", [])
    if not results:
        return {"error": f"No options data for {ticker}"}

    underlying_price = float(results[0].get("underlying_asset", {}).get("price", 0) or 0)
    if underlying_price <= 0:
        return {"error": f"Invalid underlying price for {ticker}"}

    atm_options = [
        item
        for item in results
        if abs(float(item.get("details", {}).get("strike_price", 0) or 0) - underlying_price) / underlying_price
        < 0.05
    ]
    avg_iv = sum(float(item.get("implied_volatility", 0) or 0) for item in atm_options) / max(len(atm_options), 1)
    return {
        "ticker": ticker.upper(),
        "expiry_within_days": expiry_within_days,
        "underlying_price": underlying_price,
        "atm_iv": round(avg_iv, 4),
        "total_contracts": len(results),
        "atm_contract_count": len(atm_options),
        "source": "massive",
    }


@tool
async def get_stock_quote(ticker: str) -> dict:
    """股票报价：IBKR 优先，否则 FMP。"""
    if not ticker.strip():
        return {"error": "ticker is required"}
    sym = ticker.strip().upper()
    if ibkr_conn.ibkr_is_enabled():
        await ibkr_conn.ibkr_ensure_connected()
        if ibkr_conn.ibkr_is_connected():
            q = await get_stock_quote_ibkr(sym)
            if q.get("ok"):
                last_v = q.get("last")
                bid_v = q.get("bid")
                ask_v = q.get("ask")
                close_v = q.get("close")
                mid = None
                if bid_v is not None and ask_v is not None:
                    mid = (float(bid_v) + float(ask_v)) / 2.0
                price = float(last_v) if last_v is not None else float(mid or 0)
                chg_pct = 0.0
                if close_v and price:
                    chg_pct = (price - float(close_v)) / float(close_v) * 100.0
                return {
                    "symbol": q.get("symbol"),
                    "price": price,
                    "changesPercentage": round(chg_pct, 4),
                    "volume": q.get("volume"),
                    "bid": bid_v,
                    "ask": ask_v,
                    "open": q.get("open"),
                    "high": q.get("high"),
                    "low": q.get("low"),
                    "close": close_v,
                    "market_data_type": q.get("market_data_type_label"),
                    "source": "ibkr",
                }

    def _quote() -> dict:
        data = get_fmp_client().get_quote(sym)
        if not data:
            return {"error": f"No quote for {ticker}"}
        row = dict(data)
        row["source"] = "fmp"
        return row

    try:
        return await asyncio.to_thread(_quote)
    except Exception as exc:
        return {"error": f"get_stock_quote_failed: {exc}"}


@tool
async def get_realtime_quote_ibkr(ticker: str) -> dict:
    """IBKR 股票快照。"""
    if not ticker.strip():
        return {"error": "ticker is required"}
    return await get_stock_quote_ibkr(ticker)


@tool
async def get_option_chain_params_ibkr(ticker: str) -> dict:
    """IBKR 期权到期日与行权价。"""
    if not ticker.strip():
        return {"error": "ticker is required"}
    return await ibkr_fetch_option_chain_params(ticker)


@tool
async def get_option_contract_ibkr(ticker: str, expiry: str, strike: float, right: str) -> dict:
    """IBKR 单合约行情。"""
    if not ticker.strip():
        return {"error": "ticker is required"}
    return await get_option_quote_ibkr(ticker, expiry, strike, right)


@tool
async def get_option_chain_snapshot_tool_ibkr(
    ticker: str,
    expiry: str = "",
    strikes_each_side: int = 4,
) -> dict:
    """IBKR 近 ATM 期权快照。"""
    if not ticker.strip():
        return {"error": "ticker is required"}
    exp = expiry.strip() or None
    return await get_option_chain_snapshot_ibkr(
        ticker,
        expiry=exp,
        strikes_each_side=strikes_each_side,
    )


@tool
async def get_symbol_news_ibkr(ticker: str, limit: int = 12) -> dict:
    """IBKR 新闻标题。"""
    if not ticker.strip():
        return {"error": "ticker is required"}
    lim = max(1, min(int(limit), 300))
    return await get_ibkr_news_headlines(ticker.strip().upper(), limit=lim)


@tool
async def sentiment_quantify(ticker: str, window_hours: int = 72) -> dict:
    """量化 ticker 的社交情绪 (Xpoz MCP)。"""
    if not ticker.strip():
        return {"error": "ticker is required"}
    if window_hours <= 0:
        return {"error": "window_hours must be > 0"}

    client = XpozClient()
    try:
        result = await client.get_ticker_sentiment(ticker=ticker, window_hours=window_hours)
        return {
            "ticker": ticker.upper(),
            "sentiment_score": float(result.get("sentiment_score", 0) or 0),
            "mention_count": int(result.get("mention_count", 0) or 0),
            "bullish_pct": float(result.get("bullish_pct", 0) or 0),
            "platforms": result.get("platforms", {}),
        }
    except Exception as exc:
        return {"error": f"sentiment_quantify_failed: {exc}"}
    finally:
        await client.close()
