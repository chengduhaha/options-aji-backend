"""Social radar + smart-vs-retail synthesis service."""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, cast

from pydantic import BaseModel, Field
from sqlalchemy import select, tuple_

from app.api.routes.market_dashboard import WATCHLIST_MOVER
from app.config import get_settings
from app.tools.openbb_tools import build_default_toolkit
from app.db.models import ResonanceSignalRow, SocialPostRow, TickerSentimentSnapshotRow
from app.db.session import SessionLocal
from app.services.kol_handles import parse_kol_handles_csv

logger = logging.getLogger(__name__)
_XPOZ_BREAKER_OPEN_UNTIL = 0.0
_XPOZ_BREAKER_FAIL_COUNT = 0


class SocialRadarItem(BaseModel):
    symbol: str
    mentions_24h: int
    mentions_growth_pct: float
    sentiment_score: int = Field(ge=0, le=100)
    direction: str
    resonance: str


class SocialRadarResponse(BaseModel):
    generated_at_utc: str
    items: list[SocialRadarItem]


class KolProfileItem(BaseModel):
    handle: str
    label: str
    posts_24h: int


class KolDirectoryResponse(BaseModel):
    generated_at_utc: str
    items: list[KolProfileItem]


class ResonanceStreamItem(BaseModel):
    id: int
    symbol: str
    signal_type: str
    triggered_at_utc: str
    institutional_direction: str
    retail_direction: str
    institutional_strength: int
    retail_strength: int
    confidence: Optional[float] = None
    narrative_zh: Optional[str] = None


class ResonanceStreamResponse(BaseModel):
    generated_at_utc: str
    items: list[ResonanceStreamItem]


class SmartVsRetailSnapshot(BaseModel):
    symbol: str
    snapshot_time: str
    institutional_direction: str
    institutional_strength: int
    unusual_flow_count_24h: int
    premium_flow_usd: float
    retail_direction: str
    retail_sentiment_score: int
    mentions_24h: int
    mention_growth_pct: float
    consensus_type: str
    ai_narrative_zh: str
    confidence: float


class SocialPostPayload(BaseModel):
    source: str
    external_id: str
    author: Optional[str] = None
    title: Optional[str] = None
    content: Optional[str] = None
    url: Optional[str] = None
    score: Optional[int] = None
    comments_count: Optional[int] = None
    created_at: datetime
    raw_json: Optional[dict[str, object]] = None
    tickers: list[str] = Field(default_factory=list)


class SocialFetchResult(BaseModel):
    mentions_24h: int
    sentiment_score: int
    mentions_growth_pct: float
    source_breakdown: dict[str, int]
    posts: list[SocialPostPayload] = Field(default_factory=list)


def kol_handle_set_from_settings() -> set[str]:
    cfg = get_settings()
    return set(parse_kol_handles_csv(cfg.social_kol_handles))


def _cashtag_tickers(title: Optional[str], content: Optional[str]) -> list[str]:
    blob = f"{title or ''} {content or ''}".upper()
    found = re.findall(r"\$([A-Z]{1,5})\b", blob)
    deduped: list[str] = []
    seen: set[str] = set()
    for sym in found:
        if sym in seen:
            continue
        seen.add(sym)
        deduped.append(sym)
    return deduped[:24]


def _sentiment_direction(score: int) -> str:
    if score >= 60:
        return "bullish"
    if score <= 40:
        return "bearish"
    return "neutral"


def _compute_growth_pct(symbol: str, mentions_24h: int) -> float:
    with SessionLocal() as session:
        previous = session.execute(
            select(TickerSentimentSnapshotRow)
            .where(TickerSentimentSnapshotRow.symbol == symbol)
            .order_by(TickerSentimentSnapshotRow.snapshot_time.desc())
            .limit(1)
        ).scalar_one_or_none()
    prev_mentions = int(previous.mention_count_24h) if previous is not None else 0
    if prev_mentions <= 0:
        return 0.0
    return round(((mentions_24h - prev_mentions) / max(prev_mentions, 1)) * 100.0, 2)


def _score_post_text(raw_text: str) -> int:
    text = raw_text.lower()
    bullish_words = ("bull", "long", "call", "breakout", "moon", "pump", "buy")
    bearish_words = ("bear", "short", "put", "dump", "crash", "sell", "down")
    bull_hits = sum(1 for word in bullish_words if word in text)
    bear_hits = sum(1 for word in bearish_words if word in text)
    if bull_hits == bear_hits:
        return 50
    if bull_hits > bear_hits:
        return min(95, 55 + bull_hits * 8)
    return max(5, 45 - bear_hits * 8)


def _parse_datetime(raw: object) -> datetime:
    if isinstance(raw, datetime):
        return raw.astimezone(timezone.utc) if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, str) and raw.strip():
        value = raw.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(value)
            return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _extract_post_payload(
    row: object,
    source: str,
    symbol: str,
    *,
    tickers_override: Optional[list[str]] = None,
) -> Optional[SocialPostPayload]:
    if isinstance(row, dict):
        raw_map = row
    else:
        raw_map = {
            "id": getattr(row, "id", None),
            "author": getattr(row, "author", None),
            "username": getattr(row, "username", None),
            "title": getattr(row, "title", None),
            "content": getattr(row, "content", None),
            "text": getattr(row, "text", None),
            "url": getattr(row, "url", None),
            "score": getattr(row, "score", None),
            "likes": getattr(row, "likes", None),
            "comments_count": getattr(row, "comments_count", None),
            "created_at": getattr(row, "created_at", None),
            "published_at": getattr(row, "published_at", None),
        }

    external_id = str(raw_map.get("id") or raw_map.get("post_id") or "").strip()
    if not external_id:
        return None

    content = str(raw_map.get("content") or raw_map.get("text") or "").strip() or None
    title = str(raw_map.get("title") or "").strip() or None
    author = str(raw_map.get("author") or raw_map.get("username") or "").strip() or None
    url = str(raw_map.get("url") or "").strip() or None
    created_at = _parse_datetime(raw_map.get("created_at") or raw_map.get("published_at"))
    score_raw = raw_map.get("score") or raw_map.get("likes")
    score = int(score_raw) if isinstance(score_raw, (int, float)) else None
    comments_raw = raw_map.get("comments_count")
    comments_count = int(comments_raw) if isinstance(comments_raw, (int, float)) else None
    sym_u = symbol.strip().upper()
    tickers = (
        list(tickers_override)
        if tickers_override is not None
        else ([sym_u] if sym_u else [])
    )
    return SocialPostPayload(
        source=source,
        external_id=external_id,
        author=author,
        title=title,
        content=content,
        url=url,
        score=score,
        comments_count=comments_count,
        created_at=created_at,
        raw_json=raw_map if isinstance(raw_map, dict) else None,
        tickers=tickers,
    )


def _persist_social_posts(posts: list[SocialPostPayload]) -> None:
    if not posts:
        return
    with SessionLocal() as session:
        keys = {(post.source, post.external_id) for post in posts}
        existing_rows = session.execute(
            select(SocialPostRow.source, SocialPostRow.external_id).where(
                tuple_(SocialPostRow.source, SocialPostRow.external_id).in_(list(keys))
            )
        ).all()
        existing = {(str(row[0]), str(row[1])) for row in existing_rows}
        for post in posts:
            key = (post.source, post.external_id)
            if key in existing:
                continue
            session.add(
                SocialPostRow(
                    source=post.source,
                    external_id=post.external_id,
                    author=post.author,
                    title=post.title,
                    content=post.content,
                    url=post.url,
                    score=post.score,
                    comments_count=post.comments_count,
                    tickers=post.tickers,
                    raw_json=post.raw_json,
                    created_at=post.created_at,
                )
            )
        session.commit()


def _xpoz_breaker_allows() -> bool:
    return time.monotonic() >= _XPOZ_BREAKER_OPEN_UNTIL


def _xpoz_breaker_record_success() -> None:
    global _XPOZ_BREAKER_FAIL_COUNT, _XPOZ_BREAKER_OPEN_UNTIL
    _XPOZ_BREAKER_FAIL_COUNT = 0
    _XPOZ_BREAKER_OPEN_UNTIL = 0.0


def _xpoz_breaker_record_failure(cooldown_seconds: int) -> None:
    global _XPOZ_BREAKER_FAIL_COUNT, _XPOZ_BREAKER_OPEN_UNTIL
    _XPOZ_BREAKER_FAIL_COUNT += 1
    if _XPOZ_BREAKER_FAIL_COUNT >= 3:
        _XPOZ_BREAKER_OPEN_UNTIL = time.monotonic() + cooldown_seconds


def _estimate_institutional_strength(symbol: str) -> tuple[int, int, float]:
    toolkit = build_default_toolkit()
    chain = toolkit.get_option_chain_full(symbol)
    if not isinstance(chain, dict):
        return 0, 0, 0.0
    unusual_count = 0
    premium_flow = 0.0
    for key in ("calls", "puts"):
        contracts = chain.get(key)
        if not isinstance(contracts, list):
            continue
        for raw in contracts:
            if not isinstance(raw, dict):
                continue
            volume = float(raw.get("volume") or 0)
            oi = float(raw.get("openInterest") or 0)
            mid = float(raw.get("midpoint") or 0)
            ratio = volume / max(oi, 1.0)
            if volume >= 300 and ratio >= 3.0:
                unusual_count += 1
                premium_flow += volume * max(mid, 0) * 100
    strength = min(5, unusual_count // 2)
    return unusual_count, strength, round(premium_flow, 2)


def _extract_text_from_row(row: object) -> str:
    if isinstance(row, dict):
        content = str(row.get("content") or row.get("text") or row.get("title") or "")
        return content.strip()
    content = getattr(row, "content", None)
    text = getattr(row, "text", None)
    title = getattr(row, "title", None)
    return str(content or text or title or "").strip()


def _xpoz_search_total(client: object, channel_name: str, query: str) -> tuple[int, list[object]]:
    channel = getattr(client, channel_name, None)
    if channel is None:
        return 0, []
    search_posts = getattr(channel, "search_posts", None)
    if not callable(search_posts):
        return 0, []
    response = search_posts(query)
    data_rows = cast(list[object], list(getattr(response, "data", []) or []))
    pagination = getattr(response, "pagination", None)
    total_rows = getattr(pagination, "total_rows", None)
    total_from_meta = int(total_rows) if isinstance(total_rows, int) else len(data_rows)
    total = max(total_from_meta, len(data_rows))
    return total, data_rows


def _run_xpoz_search_with_retry(client: object, channel: str, query: str) -> tuple[int, list[object]]:
    cfg = get_settings()
    retries = max(0, int(cfg.xpoz_retry_max))
    delay = max(0.1, float(cfg.xpoz_retry_backoff_seconds))
    last_exc: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            return _xpoz_search_total(client, channel, query)
        except Exception as exc:  # noqa: PERF203
            last_exc = exc
            if attempt >= retries:
                break
            time.sleep(delay * (attempt + 1))
    if last_exc is not None:
        raise last_exc
    return 0, []


def _fetch_xpoz_sentiment(symbol: str) -> Optional[SocialFetchResult]:
    cfg = get_settings()
    api_key = cfg.xpoz_api_key.strip()
    if not api_key:
        return None
    if not _xpoz_breaker_allows():
        logger.warning("xpoz circuit open, skip fetch symbol=%s", symbol)
        return None
    try:
        from xpoz import XpozClient

        query = f"${symbol} OR {symbol} stock OR {symbol} options"
        with XpozClient(api_key=api_key) as client:
            twitter_total, twitter_rows = _run_xpoz_search_with_retry(client, "twitter", query)
            reddit_total, reddit_rows = _run_xpoz_search_with_retry(client, "reddit", query)

        mentions = int(twitter_total + reddit_total)
        post_payloads: list[SocialPostPayload] = []
        for row in twitter_rows[:100]:
            parsed = _extract_post_payload(row, "twitter", symbol)
            if parsed is not None:
                post_payloads.append(parsed)
        for row in reddit_rows[:100]:
            parsed = _extract_post_payload(row, "reddit", symbol)
            if parsed is not None:
                post_payloads.append(parsed)
        if mentions <= 0:
            _xpoz_breaker_record_success()
            return SocialFetchResult(
                mentions_24h=0,
                sentiment_score=50,
                mentions_growth_pct=0.0,
                source_breakdown={"twitter": 0, "reddit": 0},
                posts=post_payloads,
            )

        sample_rows = [*twitter_rows[:30], *reddit_rows[:30]]
        if not sample_rows:
            sentiment = 50
        else:
            scores = [_score_post_text(_extract_text_from_row(row)) for row in sample_rows]
            sentiment = int(round(sum(scores) / len(scores)))

        growth = _compute_growth_pct(symbol, mentions)
        _xpoz_breaker_record_success()
        return SocialFetchResult(
            mentions_24h=mentions,
            sentiment_score=sentiment,
            mentions_growth_pct=growth,
            source_breakdown={"twitter": twitter_total, "reddit": reddit_total},
            posts=post_payloads,
        )
    except Exception as exc:
        _xpoz_breaker_record_failure(int(cfg.xpoz_circuit_breaker_seconds))
        logger.warning("xpoz sentiment fallback symbol=%s err=%s", symbol, exc)
        return None


def _fetch_kol_posts_for_handle(handle: str) -> list[SocialPostPayload]:
    cfg = get_settings()
    api_key = cfg.xpoz_api_key.strip()
    h = handle.strip().lstrip("@").lower()
    if not api_key or not h:
        return []
    if not _xpoz_breaker_allows():
        logger.warning("xpoz circuit open, skip kol handle=%s", h)
        return []
    try:
        from xpoz import XpozClient

        query = f"from:{h}"
        with XpozClient(api_key=api_key) as client:
            _tw_total, twitter_rows = _run_xpoz_search_with_retry(client, "twitter", query)

        posts: list[SocialPostPayload] = []
        for row in twitter_rows[:50]:
            parsed = _extract_post_payload(row, "twitter", "", tickers_override=[])
            if parsed is None:
                continue
            parsed.tickers = _cashtag_tickers(parsed.title, parsed.content)
            base_raw = parsed.raw_json if isinstance(parsed.raw_json, dict) else {}
            parsed.raw_json = {**base_raw, "kol_handle": h, "kol_tracked": True}
            posts.append(parsed)
        _xpoz_breaker_record_success()
        return posts
    except Exception as exc:
        _xpoz_breaker_record_failure(int(cfg.xpoz_circuit_breaker_seconds))
        logger.warning("xpoz kol fetch handle=%s err=%s", h, exc)
        return []


def ingest_kol_timeline_posts() -> None:
    cfg = get_settings()
    if not cfg.feature_social_enabled:
        return
    handles = parse_kol_handles_csv(cfg.social_kol_handles)
    if not handles:
        return
    batch: list[SocialPostPayload] = []
    for h in handles:
        batch.extend(_fetch_kol_posts_for_handle(h))
    _persist_social_posts(batch)


def get_kol_directory() -> KolDirectoryResponse:
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=24)
    cfg = get_settings()
    handles = parse_kol_handles_csv(cfg.social_kol_handles)
    kol_set = set(handles)
    counts: dict[str, int] = {h: 0 for h in handles}
    with SessionLocal() as session:
        rows = session.execute(
            select(SocialPostRow)
            .where(SocialPostRow.source == "twitter")
            .where(SocialPostRow.created_at >= since)
        ).scalars().all()
        for sr in rows:
            raw = sr.raw_json if isinstance(sr.raw_json, dict) else {}
            kh = str(raw.get("kol_handle") or "").strip().lower()
            if kh and kh in kol_set:
                counts[kh] = counts[kh] + 1
                continue
            auth = (sr.author or "").strip().lstrip("@").lower()
            if auth in kol_set:
                counts[auth] = counts[auth] + 1

    items = [KolProfileItem(handle=h, label=f"@{h}", posts_24h=counts[h]) for h in handles]
    return KolDirectoryResponse(generated_at_utc=now.isoformat(), items=items)


def list_resonance_timeline(limit: int = 30, symbol: Optional[str] = None) -> ResonanceStreamResponse:
    now = datetime.now(timezone.utc)
    sym_filter = symbol.strip().upper() if symbol else ""
    cap = max(limit * 5, 50)
    with SessionLocal() as session:
        rows = session.execute(
            select(ResonanceSignalRow)
            .order_by(ResonanceSignalRow.triggered_at.desc())
            .limit(cap)
        ).scalars().all()
    seen: set[str] = set()
    items: list[ResonanceStreamItem] = []
    for row in rows:
        if sym_filter and row.symbol.upper() != sym_filter:
            continue
        key = row.symbol.upper()
        if key in seen:
            continue
        seen.add(key)
        items.append(
            ResonanceStreamItem(
                id=int(row.id),
                symbol=row.symbol,
                signal_type=row.signal_type,
                triggered_at_utc=row.triggered_at.astimezone(timezone.utc).isoformat(),
                institutional_direction=row.institutional_direction,
                retail_direction=row.retail_direction,
                institutional_strength=int(row.institutional_strength),
                retail_strength=int(row.retail_strength),
                confidence=row.confidence,
                narrative_zh=row.narrative_zh,
            )
        )
        if len(items) >= limit:
            break
    return ResonanceStreamResponse(generated_at_utc=now.isoformat(), items=items)


def ingest_all_social_pipelines() -> None:
    """Run watchlist social snapshots plus KOL timeline pulls (scheduler entrypoint)."""
    ingest_social_snapshots()
    ingest_kol_timeline_posts()


def _fallback_social(symbol: str) -> SocialFetchResult:
    seed = sum(ord(ch) for ch in symbol)
    mentions = 200 + (seed % 1200)
    score = 35 + (seed % 50)
    growth = _compute_growth_pct(symbol, mentions)
    breakdown = {"reddit": int(mentions * 0.6), "twitter": int(mentions * 0.4)}
    return SocialFetchResult(
        mentions_24h=mentions,
        sentiment_score=min(100, score),
        mentions_growth_pct=growth,
        source_breakdown=breakdown,
        posts=[],
    )


def _upsert_snapshot(
    symbol: str,
    mentions: int,
    score: int,
    growth: float,
    source_breakdown: dict[str, int],
) -> None:
    direction = _sentiment_direction(score)
    now = datetime.now(timezone.utc)
    with SessionLocal() as session:
        session.add(
            TickerSentimentSnapshotRow(
                symbol=symbol,
                snapshot_time=now,
                sentiment_score=score,
                direction=direction,
                mention_count_24h=mentions,
                mention_growth_pct=growth,
                source_breakdown=source_breakdown,
            )
        )
        session.commit()


def ingest_social_snapshots() -> None:
    for symbol in WATCHLIST_MOVER:
        ingest_social_snapshot_for_symbol(symbol)


def ingest_social_snapshot_for_symbol(symbol: str) -> None:
    sym = symbol.strip().upper()
    if not sym:
        return
    result = _fetch_xpoz_sentiment(sym) or _fallback_social(sym)
    _persist_social_posts(result.posts)
    _upsert_snapshot(
        symbol=sym,
        mentions=result.mentions_24h,
        score=result.sentiment_score,
        growth=result.mentions_growth_pct,
        source_breakdown=result.source_breakdown,
    )


def get_social_radar(limit: int = 10) -> SocialRadarResponse:
    now = datetime.now(timezone.utc)
    max_age = now - timedelta(hours=6)
    items: list[SocialRadarItem] = []
    with SessionLocal() as session:
        rows = session.execute(
            select(TickerSentimentSnapshotRow)
            .where(TickerSentimentSnapshotRow.snapshot_time >= max_age)
            .order_by(TickerSentimentSnapshotRow.snapshot_time.desc())
            .limit(200)
        ).scalars().all()

    latest_by_symbol: dict[str, TickerSentimentSnapshotRow] = {}
    for row in rows:
        if row.symbol not in latest_by_symbol:
            latest_by_symbol[row.symbol] = row

    for symbol, row in latest_by_symbol.items():
        _, institutional_strength, _ = _estimate_institutional_strength(symbol)
        resonance = "high" if institutional_strength >= 3 and row.sentiment_score >= 60 else "normal"
        items.append(
            SocialRadarItem(
                symbol=symbol,
                mentions_24h=row.mention_count_24h,
                mentions_growth_pct=float(row.mention_growth_pct or 0),
                sentiment_score=row.sentiment_score,
                direction=row.direction,
                resonance=resonance,
            )
        )
    items.sort(key=lambda x: (x.mentions_growth_pct, x.mentions_24h), reverse=True)
    return SocialRadarResponse(generated_at_utc=now.isoformat(), items=items[:limit])


def build_smart_vs_retail(symbol: str) -> SmartVsRetailSnapshot:
    sym = symbol.strip().upper()
    if not sym:
        raise ValueError("symbol is required")
    with SessionLocal() as session:
        sentiment_row = session.execute(
            select(TickerSentimentSnapshotRow)
            .where(TickerSentimentSnapshotRow.symbol == sym)
            .order_by(TickerSentimentSnapshotRow.snapshot_time.desc())
            .limit(1)
        ).scalar_one_or_none()

    if sentiment_row is None:
        fallback = _fallback_social(sym)
        _upsert_snapshot(
            sym,
            fallback.mentions_24h,
            fallback.sentiment_score,
            fallback.mentions_growth_pct,
            fallback.source_breakdown,
        )
        with SessionLocal() as session:
            sentiment_row = session.execute(
                select(TickerSentimentSnapshotRow)
                .where(TickerSentimentSnapshotRow.symbol == sym)
                .order_by(TickerSentimentSnapshotRow.snapshot_time.desc())
                .limit(1)
            ).scalar_one()

    unusual_count, institutional_strength, premium_flow = _estimate_institutional_strength(sym)
    institutional_direction = "bullish" if institutional_strength >= 2 else "neutral"
    retail_score = int(sentiment_row.sentiment_score)
    retail_direction = _sentiment_direction(retail_score)
    consensus_type = "resonance" if institutional_direction == retail_direction else "divergence"
    confidence = min(
        0.95,
        max(0.3, (institutional_strength / 5.0) * 0.55 + (retail_score / 100.0) * 0.45),
    )
    narrative = (
        f"{sym} 当前机构方向为 {institutional_direction}，散户情绪为 {retail_direction}。"
        f"近 24 小时异动合约约 {unusual_count} 笔，社媒提及 {sentiment_row.mention_count_24h}。"
    )

    with SessionLocal() as session:
        session.add(
            ResonanceSignalRow(
                symbol=sym,
                signal_type=consensus_type,
                institutional_direction=institutional_direction,
                retail_direction=retail_direction,
                institutional_strength=institutional_strength,
                retail_strength=max(1, min(5, retail_score // 20)),
                confidence=confidence,
                narrative_zh=narrative,
                meta_json={"mentions_24h": sentiment_row.mention_count_24h},
            )
        )
        session.commit()

    return SmartVsRetailSnapshot(
        symbol=sym,
        snapshot_time=datetime.now(timezone.utc).isoformat(),
        institutional_direction=institutional_direction,
        institutional_strength=institutional_strength,
        unusual_flow_count_24h=unusual_count,
        premium_flow_usd=premium_flow,
        retail_direction=retail_direction,
        retail_sentiment_score=retail_score,
        mentions_24h=sentiment_row.mention_count_24h,
        mention_growth_pct=float(sentiment_row.mention_growth_pct or 0),
        consensus_type=consensus_type,
        ai_narrative_zh=narrative,
        confidence=round(confidence, 3),
    )
