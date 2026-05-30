"""Regression tests for MVP war-room and macro calendar insight shaping."""
from __future__ import annotations

from types import SimpleNamespace

from app.api.routes import mvp


def test_war_room_event_keeps_deep_detail_fields() -> None:
    event = mvp._normalize_war_room_event(
        {
            "title": "Fed speakers flagged higher-for-longer risk",
            "impact": "风险",
            "impact_scope": "rates",
            "related_assets": ["spy", "qqq", "tlt"],
            "impact_note_zh": "长端利率上行会压制成长股估值。",
            "watch_zh": "盯 10Y 是否继续上穿昨日高点。",
            "deep_dive_zh": "该消息影响的是折现率和风险偏好，不是单一新闻标题本身。",
            "trade_implications_zh": "开盘前避免追高高久期科技股，等待 QQQ 放量确认。",
            "scenario_zh": "若 10Y 回落，成长股可能修复；若继续上行，反弹容易被压制。",
            "risk_watch_zh": "若美元与收益率同涨，应降低裸买 call 权利金暴露。",
        }
    )

    assert event["impact_scope"] == "rates_bonds"
    assert event["related_assets"] == ["SPY", "QQQ", "TLT"]
    assert event["deep_dive_zh"].startswith("该消息影响")
    assert "QQQ" in event["trade_implications_zh"]
    assert event["scenario_zh"].startswith("若 10Y")
    assert event["risk_watch_zh"].startswith("若美元")


def test_macro_calendar_fallback_insights_are_chinese_and_actionable() -> None:
    events = [
        {
            "date": "2026-05-21T20:30:00+00:00",
            "country": "US",
            "event": "Initial Jobless Claims",
            "impact": "High",
            "estimate": 225000,
            "previous": 220000,
            "actual": None,
        }
    ]

    result = mvp._fallback_macro_calendar_insights(events)

    assert result["engine"] == "rules"
    assert result["events"][0]["title_zh"] == "初请失业金"
    assert "预期" in result["events"][0]["why_it_matters_zh"]
    assert result["market_read_zh"]
    assert result["watch_plan"]


def test_discord_fallback_event_has_deep_detail_without_llm() -> None:
    row = SimpleNamespace(
        id="evt1",
        enrichment_title_zh="美银调查：多数投资者预计美联储可能再加息",
        enrichment_summary_zh="投资者担心核心通胀抬头，可能重新推高加息预期。",
        enrichment_bullets_zh=[],
        content="Bank of America survey: investors see Fed hike risk",
        tickers=["FED", "SPY", "QQQ"],
        timestamp_utc_iso="2026-05-21T13:04:54+00:00",
    )

    event = mvp._event_from_discord(row)

    assert event["impact_scope"] == "rates_bonds"
    assert event["impact"] == "风险"
    assert event["deep_dive_zh"]
    assert event["trade_implications_zh"]
    assert event["scenario_zh"]
    assert event["risk_watch_zh"]
