"""Route registration guardrails."""

from __future__ import annotations

from app.main import create_application


def test_fusion_market_route_is_registered() -> None:
    app = create_application()

    paths = {route.path for route in app.routes}

    assert "/api/fusion/market" in paths


def test_cross_market_polymarket_and_xpoz_routes_are_registered() -> None:
    app = create_application()

    paths = {route.path for route in app.routes}

    assert "/api/cross-market/polymarket/hot" in paths
    assert "/api/cross-market/xpoz/hot" in paths
