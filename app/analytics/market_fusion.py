"""Market fusion engine — combine multiple data dimensions into actionable insights.

Takes GEX + IV + PCR + VIX + price action and outputs a structured market regime
analysis with strategy bias and risk factors.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RegimeAnalysis:
    regime: str  # "震荡" | "趋势" | "高波动" | "低波动" | "财报前" | "事件驱动"
    signal: str  # "适合卖方" | "适合买方" | "中性" | "观望"
    confidence: str  # "高" | "中" | "低"
    strategy_bias: list[str] = field(default_factory=list)
    risk_factors: list[str] = field(default_factory=list)
    summary_zh: str = ""


@dataclass
class FusionInput:
    """All data dimensions for fusion analysis."""
    symbol: str = ""
    spot_price: Optional[float] = None
    change_pct: Optional[float] = None
    vix: Optional[float] = None
    vix_change_pct: Optional[float] = None
    vix_term_structure: Optional[str] = None
    iv_rank: Optional[float] = None
    atm_iv: Optional[float] = None
    pcr_volume: Optional[float] = None
    pcr_oi: Optional[float] = None
    net_gex_bn: Optional[float] = None
    gex_regime: Optional[str] = None
    gamma_flip: Optional[float] = None
    max_pain: Optional[float] = None
    hv20: Optional[float] = None
    hv60: Optional[float] = None
    days_to_earnings: Optional[int] = None
    earnings_implied_move_pct: Optional[float] = None
    portfolio_delta: Optional[float] = None
    portfolio_gamma: Optional[float] = None
    portfolio_theta: Optional[float] = None
    portfolio_vega: Optional[float] = None


def analyze_market_regime(data: FusionInput) -> RegimeAnalysis:
    """Synthesize all input dimensions into a structured market regime."""
    clues: list[str] = []
    bullish: int = 0
    bearish: int = 0
    strategies: list[str] = []
    risks: list[str] = []

    # 1. GEX regime
    if data.net_gex_bn is not None:
        if data.net_gex_bn > 0:
            bullish += 1
            strategies.append("Iron Condor")
            strategies.append("Credit Spread")
            if data.net_gex_bn > 1:
                clues.append(f"正Gamma ${data.net_gex_bn:.1f}B → 做市商抑制波动")
        else:
            bearish += 1
            strategies.append("Trend Following")
            strategies.append("Long Straddle")
            if data.net_gex_bn < -1:
                risks.append(f"负Gamma ${abs(data.net_gex_bn):.1f}B → 波动可能放大")
                clues.append(f"负Gamma ${abs(data.net_gex_bn):.1f}B → 做市商放大波动")

    if data.gex_regime == "Negative Gamma":
        bearish += 1
        risks.append("负Gamma环境，Delta 对冲加速趋势")

    # 2. IV Rank
    if data.iv_rank is not None:
        if data.iv_rank >= 70:
            bearish += 1
            strategies.append("Short Vega (Iron Condor)")
            clues.append(f"IV Rank {data.iv_rank:.0f}% → 期权偏贵，适合卖方")
        elif data.iv_rank <= 30:
            bullish += 1
            strategies.append("Long Vega (Long Call/Put)")
            clues.append(f"IV Rank {data.iv_rank:.0f}% → 期权便宜，适合买方")

    # 3. VIX
    if data.vix is not None:
        if data.vix > 25:
            bearish += 1
            risks.append("VIX 偏高，避免裸卖期权")
        elif data.vix < 13:
            risks.append("VIX 极低，注意波动率回归")

    # 4. VIX term structure
    if data.vix_term_structure:
        if data.vix_term_structure == "Backwardation":
            bearish += 1
            risks.append("VIX Backwardation — 市场恐慌信号")
            clues.append("VIX 期限结构贴水 → 做多波动率")
        else:
            strategies.append("Short VIX Futures")

    # 5. PCR
    if data.pcr_volume is not None:
        if data.pcr_volume > 1.2:
            clues.append(f"PCR {data.pcr_volume:.2f} → Put 异常活跃（反向看涨信号）")
        elif data.pcr_volume > 1:
            bearish += 1
            clues.append(f"PCR {data.pcr_volume:.2f} → 市场偏谨慎")
        elif data.pcr_volume < 0.5:
            risks.append(f"PCR {data.pcr_volume:.2f} → Call 过热，警惕回调")
        elif data.pcr_volume < 0.7:
            bullish += 1
            clues.append(f"PCR {data.pcr_volume:.2f} → 市场偏乐观")

    # 6. Earnings
    if data.days_to_earnings is not None and data.days_to_earnings <= 14:
        strategies.append("Earnings Straddle/Strangle")
        if data.days_to_earnings <= 3:
            risks.append(f"距财报 {data.days_to_earnings} 天，IV 已定价事件溢价")
        clues.append(f"距财报 {data.days_to_earnings} 天")

    # 7. Determine regime
    net_score = bullish - bearish
    strategies = list(dict.fromkeys(strategies))

    if data.days_to_earnings is not None and 1 <= data.days_to_earnings <= 7:
        regime = "财报前"
    elif bearish >= 3:
        regime = "高波动"
    elif bullish >= 3:
        regime = "低波动"
    elif net_score > 0:
        regime = "震荡"
    elif net_score < 0:
        regime = "趋势"
    else:
        regime = "中性"

    # 8. Signal
    if net_score >= 2:
        signal = "适合卖方"
    elif net_score <= -2:
        signal = "适合买方"
    elif abs(net_score) <= 1 and net_score > 0:
        signal = "中性偏多"
    elif abs(net_score) <= 1 and net_score < 0:
        signal = "中性偏空"
    else:
        signal = "中性"

    confidence = "高" if abs(net_score) >= 3 else "中" if abs(net_score) >= 1 else "低"

    # 9. Summary
    summary_parts = clues[:3]
    summary = " · ".join(summary_parts) if summary_parts else "多维度数据无极端信号"
    if len(clues) > 3:
        summary += f"（另有 {len(clues) - 3} 个信号）"

    return RegimeAnalysis(
        regime=regime,
        signal=signal,
        confidence=confidence,
        strategy_bias=strategies,
        risk_factors=risks,
        summary_zh=summary,
    )


def analyze_position_risk(
    delta: float, gamma: float, theta: float, vega: float,
) -> RegimeAnalysis:
    """Analyze portfolio/position Greeks and output risk profile."""
    risks: list[str] = []
    strategies: list[str] = []
    clues: list[str] = []

    if abs(delta) > 500:
        risks.append(f"Delta {delta:.0f} → 方向暴露较大")
    elif abs(delta) > 100:
        clues.append(f"Delta {delta:.0f} → {('偏多' if delta > 0 else '偏空')}")

    if gamma < -0.05:
        risks.append(f"Gamma {gamma:.4f} → 负Gamma加速亏损，需警惕突发波动")
    elif gamma < -0.01:
        clues.append(f"Gamma {gamma:.4f} → 注意波动放大")
    elif gamma > 0.05:
        strategies.append("Gamma Scalping")
        clues.append(f"Gamma {gamma:.4f} → 正Gamma低买高卖机会")

    if abs(theta) > 100:
        clues.append(f"Theta {theta:+.0f}/天 → {'卖方收益可观' if theta > 0 else '时间损耗大'}")
    elif theta < 0:
        clues.append(f"Theta {theta:+.0f}/天 → 买方注意持仓周期")
    elif theta > 0:
        strategies.append("Theta Decay (Short Options)")

    if abs(vega) > 500:
        risks.append(f"Vega {vega:+.0f} → IV 变化 1% 影响 ${abs(vega):.0f}")
    elif vega > 0:
        clues.append(f"Vega {vega:+.0f} → IV 升 1% 获利 ${vega:.0f}")

    direction = "偏多" if delta > 0 else "偏空" if delta < 0 else "中性"
    side = "买方" if theta < 0 else "卖方" if theta > 0 else "中性"
    profile = f"{direction} · {side}"

    return RegimeAnalysis(
        regime=profile,
        signal="注意风险" if risks else "正常",
        confidence="中",
        strategy_bias=list(dict.fromkeys(strategies)),
        risk_factors=risks,
        summary_zh=" · ".join(clues[:3]) if clues else "组合 Greeks 无极端信号",
    )