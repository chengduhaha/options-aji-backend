"""Massive API client — Options Starter tier.

Base URL: https://api.massive.com
Auth: Authorization: Bearer {API_KEY}
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0


class MassiveClient:
    """Thin async-friendly wrapper around the Massive REST API."""

    def __init__(self, api_key: str, base_url: str = "https://api.massive.com"):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        }

    # ── low-level ──────────────────────────────────────────────────────────────

    def _get(self, path: str, params: dict | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
                resp = client.get(url, headers=self._headers, params=params or {})
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as exc:
            logger.warning("Massive HTTP %s %s: %s", exc.response.status_code, url, exc.response.text[:200])
            return {"error": str(exc), "status_code": exc.response.status_code}
        except Exception as exc:
            logger.warning("Massive request failed %s: %s", url, exc)
            return {"error": str(exc)}

    # ── Options Contracts (reference data) ────────────────────────────────────────

    def list_contracts(
        self,
        underlying_ticker: Optional[str] = None,
        contract_type: Optional[str] = None,
        expiration_date: Optional[str] = None,
        expired: bool = False,
        limit: int = 1000,
    ) -> dict[str, Any]:
        """GET /v3/reference/options/contracts"""
        params: dict[str, Any] = {"limit": limit, "expired": str(expired).lower()}
        if underlying_ticker:
            params["underlying_ticker"] = underlying_ticker.upper()
        if contract_type:
            params["contract_type"] = contract_type
        if expiration_date:
            params["expiration_date"] = expiration_date
        return self._get("/v3/reference/options/contracts", params)

    def get_contract(self, options_ticker: str) -> dict[str, Any]:
        """GET /v3/reference/options/contracts/{optionsTicker}"""
        return self._get(f"/v3/reference/options/contracts/{options_ticker}")

    # ── Options Aggregates (K-line) ─────────────────────────────────────────────

    def get_bars(
        self,
        options_ticker: str,
        multiplier: int,
        timespan: str,
        from_date: str,
        to_date: str,
        adjusted: bool = True,
        sort: str = "asc",
        limit: int = 5000,
    ) -> dict[str, Any]:
        """GET /v2/aggs/ticker/{optionsTicker}/range/{multiplier}/{timespan}/{from}/{to}"""
        path = f"/v2/aggs/ticker/{options_ticker}/range/{multiplier}/{timespan}/{from_date}/{to_date}"
        return self._get(path, {"adjusted": str(adjusted).lower(), "sort": sort, "limit": limit})

    def get_previous_day_bar(self, options_ticker: str) -> dict[str, Any]:
        """GET /v2/aggs/ticker/{optionsTicker}/prev"""
        return self._get(f"/v2/aggs/ticker/{options_ticker}/prev")

    # ── Options Snapshots ───────────────────────────────────────────────────────

    def get_option_chain_snapshot(
        self,
        underlying: str,
        contract_type: Optional[str] = None,
        expiration_date: Optional[str] = None,
        strike_price_gte: Optional[float] = None,
        strike_price_lte: Optional[float] = None,
        limit: int = 250,
    ) -> list[dict[str, Any]]:
        """GET /v3/snapshot/options/{underlyingAsset} — full chain or filtered.

        Handles pagination automatically via next_url.
        Returns flat list of contract snapshot dicts.
        """
        params: dict[str, Any] = {"limit": limit}
        if contract_type:
            params["contract_type"] = contract_type
        if expiration_date:
            params["expiration_date"] = expiration_date
        if strike_price_gte is not None:
            params["strike_price.gte"] = strike_price_gte
        if strike_price_lte is not None:
            params["strike_price.lte"] = strike_price_lte

        results: list[dict[str, Any]] = []
        url = f"{self.base_url}/v3/snapshot/options/{underlying.upper()}"

        while url:
            try:
                with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
                    resp = client.get(url, headers=self._headers, params=params if results == [] else {})
                    resp.raise_for_status()
                    data = resp.json()
            except Exception as exc:
                logger.warning("Massive chain snapshot %s: %s", underlying, exc)
                break

            results.extend(data.get("results", []))
            url = data.get("next_url", "")  # paginate
            if not url:
                break

        return results

    def get_unified_snapshot(self, tickers: list[str]) -> dict[str, Any]:
        """GET /v3/snapshot — cross-asset unified snapshot."""
        params = {"ticker.any_of": ",".join(tickers), "type": "options", "limit": 250}
        return self._get("/v3/snapshot", params)


def get_massive_client() -> MassiveClient:
    cfg = get_settings()
    return MassiveClient(api_key=cfg.massive_api_key, base_url=cfg.massive_base_url)
