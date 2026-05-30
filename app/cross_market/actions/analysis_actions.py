"""分析类 Copilot tools."""
from __future__ import annotations

from typing import Optional

from langchain_core.tools import tool

from app.cross_market.domain.probability import ProbabilityInputs, fuse_probabilities


@tool
async def probability_fusion(
    options_prob: float,
    polymarket_prob: float,
    social_prob: float,
    institutional_prob: float,
    weights: Optional[dict[str, float]] = None,
) -> dict:
    """融合四源概率并检测背离."""
    merged_weights = weights or {}
    try:
        payload = ProbabilityInputs(
            options_prob=options_prob,
            polymarket_prob=polymarket_prob,
            social_prob=social_prob,
            institutional_prob=institutional_prob,
            options_weight=float(merged_weights.get("options", 0.25)),
            polymarket_weight=float(merged_weights.get("polymarket", 0.35)),
            social_weight=float(merged_weights.get("social", 0.20)),
            institutional_weight=float(merged_weights.get("institutional", 0.20)),
        )
    except Exception as exc:
        return {"error": f"invalid_probability_inputs: {exc}"}
    return fuse_probabilities(payload).model_dump()


@tool
async def ontology_trace_impact_chain(event_keywords: list[str], depth: int = 3) -> dict:
    """根据事件关键词追溯多阶影响标的."""
    if not event_keywords:
        return {"matched": False, "affected_tickers": []}
    if depth <= 0:
        return {"error": "depth must be > 0"}

    causal_rules: dict[str, dict[str, list[str]]] = {
        "strait_closure": {
            "tier_1": ["USO", "XLE", "XOM", "CVX"],
            "tier_2": ["FRO", "STNG", "EURN"],
            "tier_3": ["GLD", "TLT"],
        },
        "fed_rate_cut": {
            "tier_1": ["TLT", "IEF", "XLF"],
            "tier_2": ["QQQ", "IWM", "ARKK"],
            "tier_3": ["GLD", "DXY"],
        },
        "earnings_beat": {
            "tier_1": [],
            "tier_2": [],
            "tier_3": [],
        },
    }

    keywords_joined = " ".join(item.lower() for item in event_keywords)
    matched_key = ""
    for rule_key in causal_rules:
        if any(token in keywords_joined for token in rule_key.split("_")):
            matched_key = rule_key
            break

    if not matched_key:
        return {"matched": False, "affected_tickers": []}

    selected = causal_rules[matched_key]
    tiers = ["tier_1", "tier_2", "tier_3"][:depth]
    affected: list[str] = []
    for tier in tiers:
        affected.extend(selected.get(tier, []))

    return {
        "matched": True,
        "rule": matched_key,
        "tier_1": selected.get("tier_1", []),
        "tier_2": selected.get("tier_2", []),
        "tier_3": selected.get("tier_3", []),
        "affected_tickers": affected,
    }
