"""Route registration guardrails."""

from __future__ import annotations

from app.main import create_application


def test_fusion_market_route_is_registered() -> None:
    app = create_application()

    paths = {route.path for route in app.routes}

    assert "/api/fusion/market" in paths

