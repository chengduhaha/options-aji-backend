"""MVP guest / trial / pro response redaction."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal

MvpTier = Literal["guest", "trial", "pro"]

_SENSITIVE_EVENT_KEYS = (
    "deep_dive_zh",
    "trade_implications_zh",
    "scenario_zh",
    "risk_watch_zh",
    "impact_note_zh",
    "watch_zh",
    "body",
    "content",
    "content_preview",
)


def mvp_envelope(tier: MvpTier, payload: dict[str, Any]) -> dict[str, Any]:
    return {"tier": tier, **payload}


def redact_war_room(payload: dict[str, Any], tier: MvpTier) -> dict[str, Any]:
    if tier == "pro":
        return payload
    out = deepcopy(payload)
    if tier == "guest":
        events = out.get("events")
        if isinstance(events, list):
            trimmed: list[Any] = []
            for raw in events[:2]:
                if not isinstance(raw, dict):
                    continue
                item = dict(raw)
                for key in _SENSITIVE_EVENT_KEYS:
                    item.pop(key, None)
                title = str(item.get("title") or item.get("title_zh") or "市场事件")
                item["title"] = title
                item["impact_note_zh"] = "登录后查看完整解读"
                trimmed.append(item)
            out["events"] = trimmed
        out["summary_zh"] = str(out.get("summary_zh") or "")[:120]
        treasury = out.get("treasury_read")
        if isinstance(treasury, dict):
            summary = str(treasury.get("summary_zh") or "")
            treasury = dict(treasury)
            treasury["summary_zh"] = summary[:80] + ("…" if len(summary) > 80 else "")
            out["treasury_read"] = treasury
        quality = out.get("data_quality")
        if isinstance(quality, dict):
            quality = dict(quality)
            quality["preview"] = True
            out["data_quality"] = quality
        return out
    return out


def redact_market_insights(payload: dict[str, Any], tier: MvpTier) -> dict[str, Any]:
    if tier == "pro":
        return payload
    out = deepcopy(payload)
    regime = out.get("regime")
    if isinstance(regime, dict):
        regime = dict(regime)
        if tier == "guest":
            regime["reasoning"] = ""
            regime["basis"] = []
            summary = str(regime.get("summary") or "")
            regime["summary"] = summary[:160] + ("…" if len(summary) > 160 else "")
        out["regime"] = regime
    if tier == "guest":
        for key in ("vix", "pcr", "vix_chart", "treasury"):
            block = out.get(key)
            if isinstance(block, dict):
                block = dict(block)
                if "interpretation" in block:
                    block["interpretation"] = "登录后查看完整解读"
                if "caption" in block:
                    block["caption"] = ""
                if "summary" in block:
                    block["summary"] = str(block.get("summary") or "")[:80]
                out[key] = block
    return out


def redact_stock_insights(payload: dict[str, Any], tier: MvpTier) -> dict[str, Any]:
    if tier == "pro":
        return payload
    out = deepcopy(payload)
    out["contracts_note"] = "升级 Pro（Access Key）后查看完整合约筛选与 Gamma 明细。"
    out["combined_insight"] = str(out.get("combined_insight") or "")[:280]
    if tier == "trial":
        moves = out.get("expected_moves")
        if isinstance(moves, list):
            out["expected_moves"] = moves[:2]
    return out


def redact_macro_calendar(payload: dict[str, Any], tier: MvpTier) -> dict[str, Any]:
    if tier in ("trial", "pro"):
        return payload
    out = deepcopy(payload)
    out["events"] = []
    out["market_read_zh"] = "登录后查看宏观日历 AI 解读。"
    out["watch_plan"] = []
    return out


def redact_playbook_hints(payload: dict[str, Any], tier: MvpTier) -> dict[str, Any]:
    if tier in ("trial", "pro"):
        return payload
    return {"topic": payload.get("topic", "screener"), "bullets": []}
