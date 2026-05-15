"""Pure helpers for unified feed social KOL matching and resonance cards."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from app.db.models import SocialPostRow


class ResonanceFeedFields(BaseModel):
    """Subset of fields needed to build a unified FeedItem for resonance rows."""

    id: str
    kind: str = "resonance"
    created_at_utc: str
    title: str
    body: str
    tickers: list[str] = Field(default_factory=list)
    sentiment: Optional[str] = None
    priority: Optional[str] = None


def social_row_matches_kol_filter(
    *,
    sr: SocialPostRow,
    kol_only: bool,
    kol_set: set[str],
) -> bool:
    if not kol_only:
        return True
    raw = sr.raw_json if isinstance(sr.raw_json, dict) else {}
    kh = str(raw.get("kol_handle") or "").strip().lower()
    tracked = bool(raw.get("kol_tracked"))
    auth = (sr.author or "").strip().lstrip("@").lower()
    return tracked or kh in kol_set or auth in kol_set


def resonance_stream_to_feed_fields(ritem: object) -> ResonanceFeedFields:
    rid = int(getattr(ritem, "id"))
    symbol = str(getattr(ritem, "symbol"))
    signal_type = str(getattr(ritem, "signal_type"))
    triggered_at_utc = str(getattr(ritem, "triggered_at_utc"))
    inst = str(getattr(ritem, "institutional_direction"))
    ret = str(getattr(ritem, "retail_direction"))
    narrative_zh = getattr(ritem, "narrative_zh", None)
    conf_raw = getattr(ritem, "confidence", None)
    title_slug = f"{symbol} · {signal_type}"
    body = (
        (str(narrative_zh).strip() if narrative_zh is not None else "")
        or f"机构 {inst} · 散户 {ret}"
    )
    if inst == "bullish" and ret == "bullish":
        sent: Optional[str] = "bullish"
    elif inst == "bearish" and ret == "bearish":
        sent = "bearish"
    else:
        sent = "neutral"
    conf = float(conf_raw) if conf_raw is not None else 0.0
    pri = "high" if conf >= 0.72 else ("medium" if conf >= 0.45 else "low")
    return ResonanceFeedFields(
        id=f"resonance-{rid}",
        created_at_utc=triggered_at_utc,
        title=title_slug[:512],
        body=body[:4000],
        tickers=[symbol],
        sentiment=sent,
        priority=pri,
    )
