"""Objective market regime taxonomy (Risk-On / Risk-Off style)."""
from __future__ import annotations

from typing import Any, Literal, TypedDict

from app.services.locale import Locale

RegimeCode = Literal["risk_off", "elevated_vol", "risk_on", "range_bound", "transitional"]

VALID_REGIME_CODES: frozenset[str] = frozenset(
    {"risk_off", "elevated_vol", "risk_on", "range_bound", "transitional"}
)

# 旧版中文标签 → 新 code（兼容缓存中的历史 LLM 输出）
LEGACY_LABEL_TO_CODE: dict[str, RegimeCode] = {
    "风险优先": "risk_off",
    "顺势进攻": "risk_on",
    "等待确认": "transitional",
}


class RegimeMeta(TypedDict):
    label: str
    summary_template: str


REGIME_CATALOG: dict[RegimeCode, RegimeMeta] = {
    "risk_off": {
        "label": "避险环境",
        "summary_template": (
            "跨资产数据显示避险偏好抬升（Risk-Off），指数承压或波动率快速上行；"
            "期权侧宜先评估敞口与对冲，而非预设方向。"
        ),
    },
    "elevated_vol": {
        "label": "高波动环境",
        "summary_template": (
            "波动率处于偏高区间，方向信号不一致；"
            "期权溢价、Gamma 与对冲成本对定价影响更大。"
        ),
    },
    "risk_on": {
        "label": "风险偏好",
        "summary_template": (
            "风险偏好处于偏积极区间（Risk-On），指数相对稳健且波动率未明显失控；"
            "可结合板块强弱与期限结构观察，不宜解读为单边做多信号。"
        ),
    },
    "range_bound": {
        "label": "中性震荡",
        "summary_template": (
            "指数与情绪指标波动有限，盘面呈区间震荡（Range-Bound）；"
            "突破方向需等待量价与波动率共同确认。"
        ),
    },
    "transitional": {
        "label": "过渡观察",
        "summary_template": (
            "指数、波动率与情绪指标存在分歧，处于过渡阶段（Transitional）；"
            "宜降低假设、等待更多交叉验证。"
        ),
    },
}

REGIME_CATALOG_EN: dict[RegimeCode, RegimeMeta] = {
    "risk_off": {
        "label": "Risk-Off",
        "summary_template": (
            "Cross-asset data shows rising risk aversion (Risk-Off); indices are weak or "
            "volatility is spiking. Options traders should review exposure and hedges before "
            "assuming direction."
        ),
    },
    "elevated_vol": {
        "label": "Elevated Vol",
        "summary_template": (
            "Volatility is elevated and directional signals are mixed. Option premiums, gamma, "
            "and hedging costs matter more for pricing."
        ),
    },
    "risk_on": {
        "label": "Risk-On",
        "summary_template": (
            "Risk appetite is constructive (Risk-On); indices are relatively stable and vol is "
            "not spiking. Watch sector leadership and term structure — not a buy signal by itself."
        ),
    },
    "range_bound": {
        "label": "Range-Bound",
        "summary_template": (
            "Indices and sentiment are range-bound with limited impulse. Wait for price/volume "
            "and volatility to confirm a breakout."
        ),
    },
    "transitional": {
        "label": "Transitional",
        "summary_template": (
            "Index, volatility, and sentiment metrics disagree — a transitional regime. Reduce "
            "assumptions until more cross-checks align."
        ),
    },
}


def regime_catalog(locale: Locale) -> dict[RegimeCode, RegimeMeta]:
    return REGIME_CATALOG_EN if locale == "en" else REGIME_CATALOG


def normalize_regime_code(code: Any, label: Any = None) -> RegimeCode | None:
    raw_code = str(code or "").strip()
    if raw_code in VALID_REGIME_CODES:
        return raw_code  # type: ignore[return-value]
    raw_label = str(label or "").strip()
    if raw_label in LEGACY_LABEL_TO_CODE:
        return LEGACY_LABEL_TO_CODE[raw_label]
    return None


def regime_label(code: RegimeCode, locale: Locale = "zh") -> str:
    return regime_catalog(locale)[code]["label"]


def classify_regime_from_metrics(
    *,
    avg_index: float,
    vix: float | None,
    vix_chg: float | None,
    vix_band: str,
    signal_score: int,
    locale: Locale = "zh",
) -> tuple[RegimeCode, str, str, str]:
    """规则分类：返回 (code, label, summary, reasoning)。"""
    band = vix_band or ""
    high_vol_band = "高波动" in band or "极端" in band or "elevated" in band.lower()

    if avg_index <= -0.35 or (vix_chg is not None and vix_chg > 3) or signal_score <= -5:
        code: RegimeCode = "risk_off"
        if locale == "en":
            triggers: list[str] = []
            if avg_index <= -0.35:
                triggers.append("index average weak")
            if vix_chg is not None and vix_chg > 3:
                triggers.append("VIX daily jump")
            if signal_score <= -5:
                triggers.append("bearish signal score")
            reasoning = f"Classified as Risk-Off: {', '.join(triggers)}."
        else:
            triggers = []
            if avg_index <= -0.35:
                triggers.append("指数均值偏弱")
            if vix_chg is not None and vix_chg > 3:
                triggers.append("VIX 日涨幅偏大")
            if signal_score <= -5:
                triggers.append("综合信号偏空")
            reasoning = f"归类为避险环境（Risk-Off）：{'、'.join(triggers)}。"
    elif (
        (vix is not None and vix >= 22)
        or (vix is not None and vix >= 18 and vix_chg is not None and vix_chg > 1.5)
        or high_vol_band
    ):
        code = "elevated_vol"
        reasoning = (
            "Classified as elevated vol: VIX level or daily change is high with mixed direction."
            if locale == "en"
            else "归类为高波动环境：VIX 水平或日变化偏高，方向信号未一致。"
        )
    elif avg_index >= 0.35 and (vix_chg is None or vix_chg < 2) and signal_score >= 0:
        code = "risk_on"
        reasoning = (
            "Classified as Risk-On: indices firm, VIX not spiking, signals not bearish."
            if locale == "en"
            else "归类为风险偏好（Risk-On）：指数偏强、VIX 未急升、信号非偏空。"
        )
    elif abs(avg_index) < 0.2 and abs(signal_score) <= 2 and (vix_chg is None or abs(vix_chg) < 1.5):
        code = "range_bound"
        reasoning = (
            "Classified as range-bound: limited index/signal movement and mild VIX change."
            if locale == "en"
            else "归类为中性震荡：指数与信号波动有限、VIX 变化温和。"
        )
    else:
        code = "transitional"
        reasoning = (
            "Classified as transitional: mixed signals, no dominant regime yet."
            if locale == "en"
            else "归类为过渡观察：指标之间存在分歧，暂无单一主导环境。"
        )

    meta = regime_catalog(locale)[code]
    return code, meta["label"], meta["summary_template"], reasoning
