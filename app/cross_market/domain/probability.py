"""Shared probability fusion utilities."""
from __future__ import annotations

from typing import Literal

import numpy as np
from pydantic import BaseModel, Field


class ProbabilityInputs(BaseModel):
    options_prob: float = Field(ge=0, le=1)
    polymarket_prob: float = Field(ge=0, le=1)
    social_prob: float = Field(ge=0, le=1)
    institutional_prob: float = Field(ge=0, le=1)
    options_weight: float = Field(default=0.25, gt=0)
    polymarket_weight: float = Field(default=0.35, gt=0)
    social_weight: float = Field(default=0.20, gt=0)
    institutional_weight: float = Field(default=0.20, gt=0)


class ProbabilityFusionResult(BaseModel):
    consensus_probability: float
    disagreement: float
    is_arbitrage_opportunity: bool
    arbitrage_direction: Literal[
        "options_overpriced",
        "options_underpriced",
        "polymarket_overpriced",
        "polymarket_underpriced",
        "social_overpriced",
        "social_underpriced",
        "institutional_overpriced",
        "institutional_underpriced",
    ]
    max_deviation_pct: float


def fuse_probabilities(inputs: ProbabilityInputs) -> ProbabilityFusionResult:
    probs = np.array(
        [
            inputs.options_prob,
            inputs.polymarket_prob,
            inputs.social_prob,
            inputs.institutional_prob,
        ],
        dtype=np.float64,
    )
    weights = np.array(
        [
            inputs.options_weight,
            inputs.polymarket_weight,
            inputs.social_weight,
            inputs.institutional_weight,
        ],
        dtype=np.float64,
    )
    weights = weights / weights.sum()

    consensus = float((probs * weights).sum())
    disagreement = float(probs.std())

    deviations = abs(probs - consensus)
    idx = int(deviations.argmax())
    source = ["options", "polymarket", "social", "institutional"][idx]
    side = "overpriced" if probs[idx] > consensus else "underpriced"

    return ProbabilityFusionResult(
        consensus_probability=round(consensus, 3),
        disagreement=round(disagreement, 3),
        is_arbitrage_opportunity=disagreement > 0.15,
        arbitrage_direction=f"{source}_{side}",  # type: ignore[arg-type]
        max_deviation_pct=round(float(deviations[idx]) * 100, 1),
    )
