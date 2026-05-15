"""Capitol Hill trading tracker — leaderboard and backtest."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.api.deps import bearer_subscription_optional
from app.clients.fmp_client import get_fmp_client
from app.config import get_settings
from app.db.models import CongressTradeRow, StockDailyBarRow
from app.db.session import db_session_dep
from app.services.cache_service import cache_get, cache_set

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/congress", tags=["congress"])

_TTL = 1800  # 30 min


def _fetch_from_fmp(chamber: str, limit: int = 200) -> list[dict]:
    """Fetch congress trades from FMP senate-trading / house-trading endpoint."""
    cfg = get_settings()
    if not cfg.fmp_api_key:
        return []
    endpoint = "/senate-trading" if chamber == "senate" else "/house-trading"
    try:
        data = get_fmp_client()._get(endpoint, {"limit": limit}) or []
        return data if isinstance(data, list) else []
    except Exception as exc:
        logger.warning("FMP congress (%s): %s", chamber, exc)
        return []


def _upsert_trades(db: Session, trades: list[dict], chamber: str) -> None:
    for t in trades:
        try:
            raw_date = str(t.get("transactionDate") or t.get("dateRecieved") or "")[:10]
            trade_date = date.fromisoformat(raw_date) if raw_date else None
            member = str(t.get("senator") or t.get("representative") or "").strip()
            symbol = str(t.get("ticker") or "").strip().upper()
            tx_type = str(t.get("type") or t.get("transactionType") or "")
            if not member or not symbol:
                continue
            exists = db.execute(
                select(CongressTradeRow).where(
                    and_(
                        CongressTradeRow.member_name == member,
                        CongressTradeRow.symbol == symbol,
                        CongressTradeRow.trade_date == trade_date,
                        CongressTradeRow.transaction_type == tx_type,
                    )
                ).limit(1)
            ).scalar_one_or_none()
            if not exists:
                db.add(CongressTradeRow(
                    member_name=member,
                    chamber=chamber,
                    symbol=symbol,
                    trade_date=trade_date,
                    transaction_type=tx_type,
                    amount_range=str(t.get("amount") or ""),
                    asset_description=str(t.get("assetDescription") or ""),
                    comment=str(t.get("comment") or ""),
                    raw_json=t,
                ))
        except Exception as exc:
            logger.debug("skip congress row: %s", exc)
    try:
        db.commit()
    except Exception:
        db.rollback()


def _price_on(db: Session, symbol: str, on: date) -> Optional[float]:
    row = db.execute(
        select(StockDailyBarRow.close)
        .where(
            and_(
                StockDailyBarRow.symbol == symbol,
                StockDailyBarRow.bar_date <= on,
            )
        )
        .order_by(StockDailyBarRow.bar_date.desc())
        .limit(1)
    ).scalar_one_or_none()
    return float(row) if row else None


def _maybe_refresh(db: Session) -> None:
    """Refresh from FMP when the trades table is sparse."""
    count = len(db.execute(select(CongressTradeRow).limit(10)).fetchall())
    if count < 5:
        senate = _fetch_from_fmp("senate", 200)
        house = _fetch_from_fmp("house", 200)
        _upsert_trades(db, senate, "senate")
        _upsert_trades(db, house, "house")


@router.get("/trades")
def get_trades(
    chamber: Optional[str] = Query(None, description="senate | house | all"),
    symbol: Optional[str] = Query(None),
    member: Optional[str] = Query(None),
    transaction_type: Optional[str] = Query(None, description="purchase | sale"),
    days: int = Query(90, ge=1, le=365),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(db_session_dep),
    _: Optional[str] = Depends(bearer_subscription_optional),
):
    """Return recent congress member trade disclosures."""
    cache_key = f"congress:trades:{chamber}:{symbol}:{member}:{transaction_type}:{days}"
    if hit := cache_get(cache_key):
        return hit

    _maybe_refresh(db)

    cutoff = date.today() - timedelta(days=days)
    q = select(CongressTradeRow).where(
        and_(
            CongressTradeRow.trade_date >= cutoff,
            CongressTradeRow.symbol != "",
        )
    )
    if chamber and chamber != "all":
        q = q.where(CongressTradeRow.chamber == chamber)
    if symbol:
        q = q.where(CongressTradeRow.symbol == symbol.upper())
    if member:
        q = q.where(CongressTradeRow.member_name.ilike(f"%{member}%"))
    if transaction_type:
        q = q.where(CongressTradeRow.transaction_type.ilike(f"%{transaction_type}%"))
    q = q.order_by(CongressTradeRow.trade_date.desc()).limit(limit)

    rows = db.execute(q).scalars().all()
    items = [
        {
            "id": r.id,
            "member": r.member_name,
            "chamber": r.chamber,
            "symbol": r.symbol,
            "date": str(r.trade_date) if r.trade_date else None,
            "type": r.transaction_type,
            "amount_range": r.amount_range,
            "asset": r.asset_description,
        }
        for r in rows
    ]
    result = {
        "items": items,
        "total": len(items),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    cache_set(cache_key, result, ttl=_TTL)
    return result


@router.get("/leaderboard")
def get_leaderboard(
    chamber: Optional[str] = Query(None, description="senate | house | all"),
    limit: int = Query(20, ge=5, le=50),
    db: Session = Depends(db_session_dep),
    _: Optional[str] = Depends(bearer_subscription_optional),
):
    """Hypothetical ROI leaderboard for congress members (buy-and-hold from disclosure date)."""
    cache_key = f"congress:leaderboard:{chamber}:{limit}"
    if hit := cache_get(cache_key):
        return hit

    _maybe_refresh(db)

    q = select(CongressTradeRow).where(
        and_(
            CongressTradeRow.symbol != "",
            CongressTradeRow.trade_date.isnot(None),
        )
    )
    if chamber and chamber != "all":
        q = q.where(CongressTradeRow.chamber == chamber)

    all_trades = db.execute(q).scalars().all()
    member_trades: dict[str, list] = {}
    for t in all_trades:
        member_trades.setdefault(t.member_name, []).append(t)

    today = date.today()
    leaderboard = []
    for member, trades in member_trades.items():
        buys = [
            t for t in trades
            if "purchase" in (t.transaction_type or "").lower()
            or "buy" in (t.transaction_type or "").lower()
        ]
        if not buys:
            continue

        returns, best_return, best_info = [], float("-inf"), None
        for t in buys:
            if not t.trade_date or not t.symbol:
                continue
            bp = _price_on(db, t.symbol, t.trade_date)
            cp = _price_on(db, t.symbol, today)
            if bp and bp > 0 and cp and cp > 0:
                ret = (cp - bp) / bp * 100
                returns.append(ret)
                if ret > best_return:
                    best_return, best_info = ret, {
                        "symbol": t.symbol,
                        "date": str(t.trade_date),
                        "return_pct": round(ret, 1),
                    }

        if not returns:
            continue

        avg_ret = sum(returns) / len(returns)
        win_rate = len([r for r in returns if r > 0]) / len(returns) * 100
        all_dates = [t.trade_date for t in trades if t.trade_date]
        years = max((today - min(all_dates)).days / 365.0, 0.1) if all_dates else 1.0
        ann = ((1 + avg_ret / 100) ** (1 / years) - 1) * 100

        leaderboard.append({
            "member": member,
            "chamber": trades[0].chamber,
            "trade_count": len(buys),
            "avg_return_pct": round(avg_ret, 1),
            "annualized_return_pct": round(ann, 1),
            "win_rate_pct": round(win_rate, 1),
            "best_trade": best_info,
        })

    leaderboard.sort(key=lambda x: x["annualized_return_pct"], reverse=True)

    result = {
        "leaderboard": leaderboard[:limit],
        "total_members": len(member_trades),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "disclaimer_zh": "⚠️ 以上为假设性计算（从披露日起以当日收盘价建仓持有至今），仅供参考，不构成投资建议。",
    }
    cache_set(cache_key, result, ttl=_TTL)
    return result


class BacktestRequest(BaseModel):
    member: Optional[str] = Field(default=None, description="议员姓名（模糊匹配）")
    chamber: Optional[str] = Field(default=None, description="senate | house")
    transaction_types: list[str] = Field(default_factory=lambda: ["purchase", "buy"])
    start_date: Optional[str] = Field(default=None, description="YYYY-MM-DD")
    end_date: Optional[str] = Field(default=None, description="YYYY-MM-DD")
    initial_capital: float = Field(default=100_000.0, ge=1_000)


@router.post("/backtest")
def congress_backtest(
    body: BacktestRequest,
    db: Session = Depends(db_session_dep),
    _: Optional[str] = Depends(bearer_subscription_optional),
):
    """Run hypothetical backtest: equal-weight all qualifying buy trades, hold to today."""
    q = select(CongressTradeRow).where(
        and_(
            CongressTradeRow.symbol != "",
            CongressTradeRow.trade_date.isnot(None),
        )
    )
    if body.member:
        q = q.where(CongressTradeRow.member_name.ilike(f"%{body.member}%"))
    if body.chamber:
        q = q.where(CongressTradeRow.chamber == body.chamber)
    if body.start_date:
        q = q.where(CongressTradeRow.trade_date >= date.fromisoformat(body.start_date))
    if body.end_date:
        q = q.where(CongressTradeRow.trade_date <= date.fromisoformat(body.end_date))
    q = q.order_by(CongressTradeRow.trade_date.asc())

    trades = db.execute(q).scalars().all()
    buys = [
        t for t in trades
        if any(tt.lower() in (t.transaction_type or "").lower() for tt in body.transaction_types)
    ]

    if not buys:
        return {
            "error": "no_trades_found",
            "message": "未找到符合条件的交易记录。请检查议员姓名或日期范围。",
            "trade_log": [],
        }

    today = date.today()
    capital = body.initial_capital
    holdings: dict[str, dict] = {}

    for t in buys:
        bp = _price_on(db, t.symbol, t.trade_date)
        if not bp or bp <= 0 or capital < 500:
            continue
        alloc = min(max(capital * 0.1, 500), capital)
        shares = alloc / bp
        holdings[t.symbol] = {
            "shares": shares,
            "buy_price": bp,
            "buy_date": t.trade_date,
            "member": t.member_name,
        }
        capital -= alloc

    trade_log = []
    total_holdings_value = 0.0
    for sym, pos in holdings.items():
        cp = _price_on(db, sym, today) or pos["buy_price"]
        value = pos["shares"] * cp
        ret = (cp - pos["buy_price"]) / pos["buy_price"] * 100
        total_holdings_value += value
        trade_log.append({
            "symbol": sym,
            "buy_date": str(pos["buy_date"]),
            "buy_price": round(pos["buy_price"], 2),
            "current_price": round(cp, 2),
            "shares": round(pos["shares"], 2),
            "return_pct": round(ret, 1),
            "member": pos["member"],
        })

    final_value = capital + total_holdings_value
    total_return = (final_value - body.initial_capital) / body.initial_capital * 100

    spy_start = _price_on(db, "SPY", buys[0].trade_date) if buys else None
    spy_end = _price_on(db, "SPY", today) if spy_start else None
    spy_return = (spy_end - spy_start) / spy_start * 100 if spy_start and spy_end and spy_start > 0 else None

    trade_log.sort(key=lambda x: x["return_pct"], reverse=True)
    return {
        "initial_capital": body.initial_capital,
        "final_value": round(final_value, 2),
        "total_return_pct": round(total_return, 1),
        "vs_spy_return_pct": round(spy_return, 1) if spy_return is not None else None,
        "trade_count": len(trade_log),
        "trade_log": trade_log[:50],
        "disclaimer_zh": "⚠️ 以上为假设性回测结果（等权分配，持有至今），不构成投资建议。过去表现不代表未来。",
    }
