"""Synthetic signal feed backed by OpenBBToolkit (yfinance Phase1 + optional GEX upstream)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional, cast

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.deps import bearer_subscription_optional
from app.tools.openbb_tools import OpenBBToolkit, build_default_toolkit

router = APIRouter(tags=["signals"])

SignalPriority = Literal["urgent", "high", "medium", "low"]
SignalDir = Literal["bull", "bear", "neut"]
SignalKind = Literal["gex", "flow", "news", "macro", "strategy"]


class SignalCard(BaseModel):
    id: str
    type: SignalKind
    priority: SignalPriority
    tag: str
    time_cn: str
    title: str
    ticker: str
    direction: SignalDir
    strength: int = Field(ge=1, le=5)
    summary: str


class SignalsFeedEnvelope(BaseModel):
    generated_at_utc: str
    source: str
    signals: list[SignalCard]


def _dir_strength_pct(pct: Optional[float]) -> tuple[SignalDir, int]:
    if pct is None:
        return cast(SignalDir, "neut"), 2

    magnitude = abs(pct)
    if magnitude >= 3.5:
        strength = 5
    elif magnitude >= 2:
        strength = 4
    elif magnitude >= 1:
        strength = 3
    elif magnitude >= 0.35:
        strength = 2
    else:
        strength = 1

    direction: SignalDir = (
        cast(SignalDir, "bull")
        if pct > 0.15
        else (cast(SignalDir, "bear") if pct < -0.15 else cast(SignalDir, "neut"))
    )
    if direction == cast(SignalDir, "neut"):
        strength = max(strength - 1, 1)
    return direction, strength


def _finalize_priority(direction: SignalDir, strength: int, pct: Optional[float]) -> SignalPriority:
    if pct is not None and abs(pct) >= 4.5:
        return cast(SignalPriority, "urgent")
    if strength >= 5 and direction != cast(SignalDir, "neut"):
        return cast(SignalPriority, "high")
    if strength >= 4:
        return cast(SignalPriority, "medium")
    return cast(SignalPriority, "low")


def _build_equity_cards(tk: OpenBBToolkit, ticker: str) -> list[SignalCard]:
    qt = tk.get_quote(ticker)
    bar = tk.frontend_market_bar(ticker)
    gex = tk.get_gex(ticker)

    last = qt.get("last_price")
    chg_pct = qt.get("change_pct")
    pct_float: Optional[float] = None
    if isinstance(chg_pct, (int, float)):
        pct_float = float(chg_pct)

    direction, strength = _dir_strength_pct(pct_float)

    atm_iv = bar.get("atmIv") if isinstance(bar, dict) else None
    pcr_raw = bar.get("pcr") if isinstance(bar, dict) else None
    atm_s = ""
    if isinstance(atm_iv, (int, float)):
        atm_s = f"，ATM IV 约 {float(atm_iv):.1f}%（近月）"
    pcr_s = ""
    if isinstance(pcr_raw, (int, float)):
        pcr_s = f"，Put/Call Volume 比值约 {float(pcr_raw):.2f}"

    price_s = ""
    if isinstance(last, (int, float)):
        price_s = f"最新价约 {float(last)}"
    pct_s = ""
    if pct_float is not None:
        pct_s = f"，较前收约 {pct_float:+.2f}%"

    gex_avail = isinstance(gex, dict) and (
        gex.get("available") is True or "netGex" in gex
    )
    card_type: SignalKind = cast(SignalKind, "gex" if gex_avail else "strategy")

    gex_lines = ""
    if gex_avail and isinstance(gex, dict):
        clipped: list[str] = []
        for key in sorted(gex.keys()):
            if key in {"symbol", "available"}:
                continue
            clipped.append(f"  · {key}: {gex[key]}")
            if len(clipped) >= 8:
                break
        gex_lines = "\n" + ("\n".join(clipped) if clipped else "  （上游未返回可读字段）")
    elif isinstance(gex, dict) and isinstance(gex.get("hint"), str):
        hint = str(gex.get("hint"))
        gex_lines = f"\n提示：{hint}（默认已启用本地 GEX 估计；可选配置 GEX_BACKEND_URL 覆盖上游）。"
    else:
        gex_lines = "\nGEX：未配置远端或未返回结构化数据（当前以现价 / 期权链摘要为主）。"

    title = (
        f"{ticker} GEX / 做市商环境摘要（上游）"
        if gex_avail
        else f"{ticker} 市场快照 · yfinance / OpenBBToolkit"
    )

    tag = "GEX" if card_type == cast(SignalKind, "gex") else "Market"

    summary = (
        "实时数据（数据源 yfinance）："
        + (price_s or "价格暂不可用")
        + pct_s
        + atm_s
        + pcr_s
        + gex_lines
    )

    now_hm = datetime.now(timezone.utc).strftime("%H:%M UTC")
    priority = _finalize_priority(direction, strength, pct_float)

    return [
        SignalCard(
            id=f"eq-{ticker.lower()}",
            type=card_type,
            priority=priority,
            tag=tag,
            time_cn=f"数据源实时 · {now_hm}",
            title=title,
            ticker=ticker,
            direction=direction,
            strength=strength,
            summary=summary,
        )
    ]


def _vix_macro_card(tk: OpenBBToolkit) -> SignalCard:
    qt = tk.get_quote("^VIX")

    pct_val = qt.get("change_pct")
    pct_float: Optional[float] = None
    if isinstance(pct_val, (int, float)):
        pct_float = float(pct_val)

    direction, strength = _dir_strength_pct(pct_float)
    last = qt.get("last_price")

    pct_text = ""
    if pct_float is not None:
        pct_text = f"，日内变化约 {pct_float:+.2f}%"

    last_text = ""
    if isinstance(last, (int, float)):
        last_text = f"VIX ~{float(last):.2f}"

    prio: SignalPriority
    if pct_float is not None and pct_float >= 8:
        prio = cast(SignalPriority, "urgent")
    elif strength >= 4:
        prio = cast(SignalPriority, "high")
    elif strength >= 2:
        prio = cast(SignalPriority, "medium")
    else:
        prio = cast(SignalPriority, "low")

    now_hm = datetime.now(timezone.utc).strftime("%H:%M UTC")

    return SignalCard(
        id="macro-vix",
        type=cast(SignalKind, "macro"),
        priority=prio,
        tag="VOL",
        time_cn=f"数据源实时 · {now_hm}",
        title="VIX · 市场整体波动风险偏好",
        ticker="^VIX",
        direction=direction,
        strength=strength,
        summary=(
            "VIX（yfinance）："
            + (last_text or "暂无有效报价")
            + pct_text
            + "。\n隐含波动高企时通常伴随期权做市商 Gamma 结构与对冲路径更陡峭，可对齐 GEX Dashboard。"
        ),
    )


@router.get("/api/signals/feed")
def signals_feed(_: Optional[str] = Depends(bearer_subscription_optional)) -> SignalsFeedEnvelope:
    toolkit = build_default_toolkit()

    equities = ["SPY", "QQQ", "NVDA"]
    merged: list[SignalCard] = [_vix_macro_card(toolkit)]
    for symbol in equities:
        merged.extend(_build_equity_cards(toolkit, symbol))

    return SignalsFeedEnvelope(
        generated_at_utc=datetime.now(timezone.utc).isoformat(),
        source="openbb_toolkit:yfinance[+optional_gex_upstream]",
        signals=merged,
    )
