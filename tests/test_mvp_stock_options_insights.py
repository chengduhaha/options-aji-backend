"""Regression tests for MVP stock options insight context handling."""
from __future__ import annotations

import pytest

from app.services import mvp_stock_options_agent as agent
from app.services.mvp_stock_options_agent import StockOptionsInsightRequest


@pytest.mark.asyncio
async def test_stock_options_insights_backfills_missing_market_regime(monkeypatch) -> None:
    """A first page load may request stock insights before frontend marketInsights is ready."""

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(agent, "has_llm_provider", lambda: False)
    monkeypatch.setattr(agent, "cache_get", lambda _key: None)
    monkeypatch.setattr(agent, "cache_set", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        agent,
        "build_mvp_market_context",
        lambda: {
            "overview": {
                "pulse": [
                    {"symbol": "SPY", "changePct": 0.8},
                    {"symbol": "QQQ", "changePct": 0.7},
                ],
                "volatility": {"vix": 15.2, "vixChangePct": -1.1, "band": "正常(15-20)"},
                "liquidity": {"putCallRatioVolumeApprox": 0.72},
            },
            "signals": {"signals": []},
            "treasury": {"rates": []},
        },
        raising=False,
    )

    result = await agent.generate_stock_options_insights(
        StockOptionsInsightRequest(
            symbol="SPY",
            direction="bull",
            spot=741.25,
            iv_rank=None,
            expected_moves=[],
            contracts=[],
            unusual_items=[],
            market_regime_code=None,
            market_regime_label=None,
        )
    )

    assert "未提供" not in result.combined_insight
    assert "风险偏好" in result.combined_insight


def test_fast_stock_options_insights_does_not_build_llm_agent(monkeypatch) -> None:
    called = False

    def fail_build_agent():
        nonlocal called
        called = True
        raise AssertionError("fast path must not build the LLM agent")

    monkeypatch.setattr(agent, "_build_agent", fail_build_agent)
    monkeypatch.setattr(
        agent,
        "build_mvp_market_context",
        lambda: {"signals": {"market_regime": "risk_on"}},
    )

    result = agent.generate_fast_stock_options_insights(
        StockOptionsInsightRequest(
            symbol="SPY",
            direction="bull",
            spot=500,
            contracts=[],
            expected_moves=[],
        )
    )

    assert called is False
    assert result.engine == "rules"
    assert "未提供" not in result.combined_insight
