"""Market intelligence sync pipelines — sectors, movers, macro, news, insider, congress."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.clients.fmp_client import get_fmp_client
from app.config import get_settings
from app.db.models import (
    AnalystRatingRow, CongressTradeRow, EarningsCalendarRow,
    InsiderTradeRow, MacroCalendarRow, SectorPerformanceRow,
    StockNewsRow, TreasuryRateRow,
)
from app.db.session import SessionLocal
from app.services.cache_service import (
    TTL_HOT, TTL_WARM, TTL_COLD,
    cache_set,
    key_market_sectors, key_market_gainers, key_market_losers, key_market_actives,
    key_macro_calendar, key_treasury_rates, key_earnings_calendar,
    key_stock_news, key_insider_latest, key_congress_latest, key_analyst_ratings,
)

logger = logging.getLogger(__name__)


def _fmp() -> object:
    return get_fmp_client()


# ── Sectors & Movers ───────────────────────────────────────────────────────────────

def sync_sectors_pipeline() -> None:
    cfg = get_settings()
    if not cfg.fmp_api_key:
        return
    client = get_fmp_client()
    session = SessionLocal()
    try:
        sectors = client.get_sector_performance()
        pe_data = client.get_sector_pe()

        pe_map = {item.get("sector", ""): item.get("pe") for item in pe_data}
        today = datetime.now(timezone.utc).date()

        for s in sectors:
            sector_name = s.get("sector", "")
            change_pct = s.get("changesPercentage") or s.get("change") or s.get("value")
            try:
                change_f = float(str(change_pct).replace("%", "")) if change_pct else None
            except ValueError:
                change_f = None

            existing = session.get(SectorPerformanceRow, (today, sector_name))
            if existing:
                existing.change_pct_1d = change_f
                existing.pe_ratio = pe_map.get(sector_name)
                existing.synced_at = datetime.now(timezone.utc)
            else:
                session.add(SectorPerformanceRow(
                    snapshot_date=today,
                    sector=sector_name,
                    change_pct_1d=change_f,
                    pe_ratio=pe_map.get(sector_name),
                    synced_at=datetime.now(timezone.utc),
                ))

        session.commit()
        cache_set(key_market_sectors(), {
            "sectors": sectors,
            "pe": pe_data,
            "synced_at": datetime.now(timezone.utc).isoformat(),
        }, ttl=TTL_HOT)
        logger.info("Sectors sync: %d sectors", len(sectors))
    except Exception as exc:
        session.rollback()
        logger.warning("Sectors sync failed: %s", exc)
    finally:
        session.close()


def sync_movers_pipeline() -> None:
    cfg = get_settings()
    if not cfg.fmp_api_key:
        return
    client = get_fmp_client()
    try:
        gainers = client.get_gainers()
        losers = client.get_losers()
        actives = client.get_most_actives()
        ts = datetime.now(timezone.utc).isoformat()
        cache_set(key_market_gainers(), {"data": gainers, "synced_at": ts}, ttl=300)
        cache_set(key_market_losers(), {"data": losers, "synced_at": ts}, ttl=300)
        cache_set(key_market_actives(), {"data": actives, "synced_at": ts}, ttl=300)
        logger.info("Movers sync done")
    except Exception as exc:
        logger.warning("Movers sync failed: %s", exc)


# ── Macro ────────────────────────────────────────────────────────────────────────

def sync_macro_calendar_pipeline() -> None:
    cfg = get_settings()
    if not cfg.fmp_api_key:
        return
    client = get_fmp_client()
    session = SessionLocal()
    try:
        today = datetime.now(timezone.utc)
        from_date = (today - timedelta(days=7)).strftime("%Y-%m-%d")
        to_date = (today + timedelta(days=30)).strftime("%Y-%m-%d")
        events = client.get_economic_calendar(from_date, to_date)

        for ev in events:
            ev_date = ev.get("date")
            if not ev_date:
                continue
            try:
                dt = datetime.fromisoformat(ev_date.replace("Z", "+00:00"))
            except ValueError:
                continue

            row = MacroCalendarRow(
                event_date=dt,
                country=ev.get("country"),
                event_name=ev.get("event"),
                impact=ev.get("impact"),
                estimate=ev.get("estimate"),
                previous=ev.get("previous"),
                actual=ev.get("actual"),
                synced_at=datetime.now(timezone.utc),
            )
            session.merge(row)

        session.commit()
        date_range = f"{from_date}_{to_date}"
        cache_set(key_macro_calendar(date_range), {
            "events": events,
            "from": from_date,
            "to": to_date,
            "synced_at": today.isoformat(),
        }, ttl=TTL_WARM)
        logger.info("Macro calendar sync: %d events", len(events))
    except Exception as exc:
        session.rollback()
        logger.warning("Macro calendar sync failed: %s", exc)
    finally:
        session.close()


def sync_treasury_rates_pipeline() -> None:
    cfg = get_settings()
    if not cfg.fmp_api_key:
        return
    client = get_fmp_client()
    session = SessionLocal()
    try:
        today = datetime.now(timezone.utc)
        from_date = (today - timedelta(days=30)).strftime("%Y-%m-%d")
        rates = client.get_treasury_rates(from_date=from_date)

        for r in rates:
            date_str = r.get("date")
            if not date_str:
                continue
            try:
                rate_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                continue

            existing = session.get(TreasuryRateRow, rate_date)
            if not existing:
                session.add(TreasuryRateRow(
                    rate_date=rate_date,
                    month1=r.get("month1"),
                    month2=r.get("month2"),
                    month3=r.get("month3"),
                    month6=r.get("month6"),
                    year1=r.get("year1"),
                    year2=r.get("year2"),
                    year5=r.get("year5"),
                    year10=r.get("year10"),
                    year30=r.get("year30"),
                    synced_at=datetime.now(timezone.utc),
                ))

        session.commit()
        cache_set(key_treasury_rates(), {"rates": rates[:30], "synced_at": today.isoformat()}, ttl=TTL_WARM)
        logger.info("Treasury rates sync: %d entries", len(rates))
    except Exception as exc:
        session.rollback()
        logger.warning("Treasury rates sync failed: %s", exc)
    finally:
        session.close()


def sync_earnings_calendar_pipeline() -> None:
    cfg = get_settings()
    if not cfg.fmp_api_key:
        return
    client = get_fmp_client()
    session = SessionLocal()
    try:
        today = datetime.now(timezone.utc)
        from_date = today.strftime("%Y-%m-%d")
        to_date = (today + timedelta(days=30)).strftime("%Y-%m-%d")
        events = client.get_earnings_calendar(from_date, to_date)

        for ev in events:
            symbol = ev.get("symbol", "").upper()
            date_str = ev.get("date")
            if not symbol or not date_str:
                continue
            try:
                e_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                continue

            existing = session.get(EarningsCalendarRow, (symbol, e_date))
            if existing:
                existing.eps_estimate = ev.get("epsEstimated")
                existing.time = ev.get("time")
                existing.is_confirmed = bool(ev.get("updatedFromDate"))
                existing.synced_at = datetime.now(timezone.utc)
            else:
                session.add(EarningsCalendarRow(
                    symbol=symbol,
                    earnings_date=e_date,
                    eps_estimate=ev.get("epsEstimated"),
                    time=ev.get("time"),
                    is_confirmed=bool(ev.get("updatedFromDate")),
                    synced_at=datetime.now(timezone.utc),
                ))

        session.commit()
        date_range = f"{from_date}_{to_date}"
        cache_set(key_earnings_calendar(date_range), {
            "events": events,
            "from": from_date,
            "to": to_date,
            "synced_at": today.isoformat(),
        }, ttl=TTL_WARM)
        logger.info("Earnings calendar sync: %d events", len(events))
    except Exception as exc:
        session.rollback()
        logger.warning("Earnings calendar sync failed: %s", exc)
    finally:
        session.close()


def sync_news_pipeline() -> None:
    cfg = get_settings()
    if not cfg.fmp_api_key:
        return
    client = get_fmp_client()
    session = SessionLocal()
    try:
        symbols = cfg.sync_watchlist_symbols
        # Fetch news for all watchlist symbols in one call
        articles = client.get_stock_news(tickers=symbols, limit=50)

        for a in articles:
            published_str = a.get("publishedDate") or a.get("date")
            if not published_str:
                continue
            try:
                pub_dt = datetime.fromisoformat(published_str.replace("Z", "+00:00"))
            except ValueError:
                pub_dt = datetime.now(timezone.utc)

            url = a.get("url", "")
            # Skip duplicates by URL
            from sqlalchemy import select
            existing = session.execute(
                select(StockNewsRow).where(StockNewsRow.url == url)
            ).scalar_one_or_none()
            if existing:
                continue

            tickers_raw = a.get("tickers") or a.get("symbol", "")
            tickers_list = tickers_raw if isinstance(tickers_raw, list) else [tickers_raw]

            session.add(StockNewsRow(
                symbols=[t.upper() for t in tickers_list if t],
                title=a.get("title", ""),
                content=a.get("text") or a.get("content"),
                url=url,
                source=a.get("site") or a.get("source"),
                published_at=pub_dt,
                synced_at=datetime.now(timezone.utc),
            ))

        session.commit()
        logger.info("News sync: %d articles processed", len(articles))
    except Exception as exc:
        session.rollback()
        logger.warning("News sync failed: %s", exc)
    finally:
        session.close()


def sync_insider_trades_pipeline() -> None:
    cfg = get_settings()
    if not cfg.fmp_api_key:
        return
    client = get_fmp_client()
    session = SessionLocal()
    try:
        trades = client.get_insider_trading_latest(page=0)
        ts = datetime.now(timezone.utc)

        for t in trades:
            symbol = (t.get("symbol") or "").upper()
            date_str = t.get("transactionDate")
            try:
                t_date = datetime.strptime(date_str, "%Y-%m-%d").date() if date_str else None
            except ValueError:
                t_date = None

            session.add(InsiderTradeRow(
                symbol=symbol,
                filer_name=t.get("reportingName"),
                filer_relation=t.get("typeOfOwner"),
                transaction_type=t.get("transactionType"),
                transaction_date=t_date,
                shares=t.get("securitiesTransacted"),
                price_per_share=t.get("price"),
                total_value=(
                    int(t["securitiesTransacted"] * t["price"])
                    if t.get("securitiesTransacted") and t.get("price") else None
                ),
                shares_owned_after=t.get("securitiesOwned"),
                filing_date=None,
                synced_at=ts,
            ))

        session.commit()
        cache_set(key_insider_latest(), {"trades": trades[:50], "synced_at": ts.isoformat()}, ttl=TTL_WARM)
        logger.info("Insider trades sync: %d records", len(trades))
    except Exception as exc:
        session.rollback()
        logger.warning("Insider trades sync failed: %s", exc)
    finally:
        session.close()


def sync_congress_trades_pipeline() -> None:
    cfg = get_settings()
    if not cfg.fmp_api_key:
        return
    client = get_fmp_client()
    session = SessionLocal()
    try:
        senate = client.get_senate_latest_trading(page=0)
        house = client.get_house_latest_trading(page=0)
        ts = datetime.now(timezone.utc)

        all_trades = [("senate", senate), ("house", house)]
        total = 0
        for chamber, trades in all_trades:
            for t in trades:
                date_str = t.get("transactionDate")
                try:
                    t_date = datetime.strptime(date_str, "%Y-%m-%d").date() if date_str else None
                except ValueError:
                    t_date = None
                filing_str = t.get("disclosureDate")
                try:
                    f_date = datetime.strptime(filing_str, "%Y-%m-%d").date() if filing_str else None
                except ValueError:
                    f_date = None

                session.add(CongressTradeRow(
                    chamber=chamber,
                    member_name=t.get("representative") or t.get("senator"),
                    symbol=t.get("ticker", "").upper() or None,
                    asset_description=t.get("assetDescription"),
                    transaction_type=t.get("type"),
                    transaction_date=t_date,
                    amount_range=t.get("amount"),
                    filing_date=f_date,
                    raw_json=t,
                    synced_at=ts,
                ))
                total += 1

        session.commit()
        combined = senate[:25] + house[:25]
        cache_set(key_congress_latest(), {"trades": combined, "synced_at": ts.isoformat()}, ttl=TTL_WARM)
        logger.info("Congress trades sync: %d records", total)
    except Exception as exc:
        session.rollback()
        logger.warning("Congress trades sync failed: %s", exc)
    finally:
        session.close()


def sync_analyst_ratings_pipeline() -> None:
    cfg = get_settings()
    if not cfg.fmp_api_key:
        return
    client = get_fmp_client()
    session = SessionLocal()
    ts = datetime.now(timezone.utc)
    try:
        for symbol in cfg.sync_watchlist_symbols:
            ratings = client.get_analyst_ratings(symbol)
            for r in ratings[:20]:  # latest 20 per symbol
                date_str = r.get("date") or r.get("publishedDate")
                try:
                    r_date = datetime.strptime(date_str, "%Y-%m-%d").date() if date_str else None
                except ValueError:
                    r_date = None

                session.add(AnalystRatingRow(
                    symbol=symbol,
                    analyst_company=r.get("gradingCompany"),
                    rating_action=r.get("action"),
                    rating_from=r.get("previousGrade"),
                    rating_to=r.get("newGrade"),
                    price_target=r.get("priceTarget"),
                    rating_date=r_date,
                    synced_at=ts,
                ))

            session.commit()
            cache_set(
                key_analyst_ratings(symbol),
                {"symbol": symbol, "ratings": ratings[:30], "synced_at": ts.isoformat()},
                ttl=TTL_WARM,
            )

        logger.info("Analyst ratings sync done")
    except Exception as exc:
        session.rollback()
        logger.warning("Analyst ratings sync failed: %s", exc)
    finally:
        session.close()
