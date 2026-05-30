"""Sync company profile reference data from FMP."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.clients.fmp_client import get_fmp_client
from app.config import get_settings
from app.db.models import CompanyProfileRow
from app.db.session import SessionLocal

logger = logging.getLogger(__name__)


def _parse_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _parse_date(value: object):
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError:
        return None


def _profile_row_data(profile: dict[str, object], peers: list[str]) -> dict[str, object]:
    return {
        "company_name": profile.get("companyName") or profile.get("company_name"),
        "industry": profile.get("industry"),
        "sector": profile.get("sector"),
        "description": profile.get("description"),
        "ceo": profile.get("ceo"),
        "employees": _parse_int(profile.get("fullTimeEmployees") or profile.get("employees")),
        "website": profile.get("website"),
        "image_url": profile.get("image") or profile.get("image_url"),
        "ipo_date": _parse_date(profile.get("ipoDate") or profile.get("ipo_date")),
        "market_cap": _parse_int(profile.get("mktCap") or profile.get("marketCap")),
        "is_etf": bool(profile.get("isEtf") or profile.get("is_etf")),
        "exchange": profile.get("exchangeShortName") or profile.get("exchange"),
        "country": profile.get("country"),
        "raw_json": {**profile, "peers": peers},
        "synced_at": datetime.now(timezone.utc),
    }


def sync_company_profiles_pipeline() -> None:
    """Fetch watchlist company profiles and peers, then upsert CompanyProfileRow."""
    cfg = get_settings()
    if not cfg.fmp_api_key:
        return

    client = get_fmp_client()
    session = SessionLocal()
    try:
        for symbol in cfg.sync_watchlist_symbols:
            sym = symbol.strip().upper()
            if not sym:
                continue
            profile = client.get_profile(sym)
            if not isinstance(profile, dict) or not profile:
                continue
            peers = client.get_peers(sym)
            row_data = _profile_row_data(profile, peers if isinstance(peers, list) else [])

            existing = session.get(CompanyProfileRow, sym)
            if existing:
                for field, value in row_data.items():
                    setattr(existing, field, value)
            else:
                session.add(CompanyProfileRow(symbol=sym, **row_data))

        session.commit()
        logger.info("Company profiles sync done")
    except Exception as exc:
        session.rollback()
        logger.warning("Company profiles sync failed: %s", exc)
    finally:
        session.close()
