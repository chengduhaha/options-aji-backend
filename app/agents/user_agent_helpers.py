"""Shared helpers for user-agent ingestion (Discord + ticker)."""

from __future__ import annotations

from typing import Optional

from app.config import Settings
from app.db.session import SessionLocal
from app.ingest.message_store import StoredDiscordMessage, list_messages_recent
from app.ingest.tickers import extract_tickers


def infer_message_filter_symbol(*, question: str, ticker_hint: str) -> tuple[str, Optional[str]]:
    """Return `(resolved_trade_symbol, discord_filter_symbol | None)`."""

    raw_hint = (ticker_hint or "").strip().upper()

    inferred_spotlight = ""
    if raw_hint:
        inferred_spotlight = raw_hint
    else:
        discovered = extract_tickers(question.strip())
        inferred_spotlight = discovered[0] if discovered else "SPY"

    filter_sym: Optional[str] = None
    if raw_hint:
        filter_sym = raw_hint
    else:
        lone = extract_tickers(question.strip())
        if len(lone) == 1:
            filter_sym = lone[0]

    return inferred_spotlight, filter_sym


def format_discord_digest(*, filter_sym: Optional[str], cfg: Settings) -> tuple[str, int]:
    """Return digest text + approximate row volume used."""

    hours = cfg.agent_discord_context_hours
    budget = cfg.agent_discord_context_limit

    try:
        with SessionLocal() as session:
            raw_entries: list[StoredDiscordMessage] = list_messages_recent(
                session,
                ticker=filter_sym,
                hours=hours,
                limit=budget,
            )
    except Exception as exc:
        return (f"[Discord存档读取异常: {type(exc).__name__}]", 0)

    if not raw_entries:
        filler = filter_sym or "全局"
        return (
            f"当前窗口内暂无 Discord 存档（筛选={filler}，回看 {hours}h）。",
            0,
        )

    lines: list[str] = []

    for counter, entity in enumerate(reversed(raw_entries), start=1):
        author_label = entity.author or "?"
        tickers_txt = ",".join(entity.tickers or []) or "—"
        clipped = (entity.content or "").replace("\n", " ").strip()
        clipped = clipped if len(clipped) <= 360 else clipped[:360] + "…"
        lines.append(
            f"{counter}. [{entity.timestamp_utc_iso}] {author_label}"
            f" · ticks={tickers_txt} · {clipped}",
        )

    digest = "[Discord存档摘要]\n" + "\n".join(lines)

    if len(digest.encode("utf-8")) > 10_500:
        digest = digest[:10_480] + "…（截断）"

    return digest, len(raw_entries)
