"""Aggregated probes for Discord ingest + option data (developer / ops visibility)."""

from __future__ import annotations

from datetime import datetime, timezone

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import DiscordMessageRow
from app.db.session import db_session_dep
from app.ingest.discord_bot import parse_channel_ids
from app.tools.openbb_tools import OpenBBToolkit, build_default_toolkit

router = APIRouter(tags=["integration"])


class DiscordPreview(BaseModel):
    id: str
    channel_id: str
    author: Optional[str] = None
    timestamp: str
    tickers: list[str]
    content_preview: str


class DiscordIngest(BaseModel):
    discord_listener_setting: bool = Field(description="ENABLE_DISCORD_LISTENER equivalent")
    token_present: bool
    channel_ids: list[str]
    channel_ids_count: int
    newest_message_age_seconds: Optional[float] = Field(
        default=None,
        description="Approx age if DB has rows; None if empty",
    )
    stored_message_count_total: int
    recent_preview: list[DiscordPreview]

    hints: list[str] = Field(
        default_factory=list,
        description="Actionable reminders when ingestion likely idle",
    )


class OptionsProbe(BaseModel):
    symbol: str
    quote: dict[str, object]
    option_chain_summary: dict[str, object]


class IntegrationEnvelope(BaseModel):
    generated_at_utc: str
    discord: DiscordIngest
    options_via_yfinance: OptionsProbe


def _preview_hints(
    *,
    cfg: Settings,
    total: int,
    newest_age: Optional[float],
) -> list[str]:
    hints: list[str] = []
    chans = parse_channel_ids(cfg.discord_channel_ids)

    if not cfg.discord_bot_token.strip():
        hints.append("未配置 DISCORD_BOT_TOKEN，Discord 网关不会上线。")

    if not chans:
        hints.append("未配置 DISCORD_CHANNEL_IDS，机器人不会存档任何频道。")

    if not cfg.enable_discord_listener:
        hints.append("ENABLE_DISCORD_LISTENER=false，已跳过 Discord 网关任务启动。")

    if cfg.discord_bot_token.strip() and chans and cfg.enable_discord_listener and total == 0:
        hints.append(
            "环境与频道已就绪但数据库暂无消息——请核对 Bot "
            "已加入服务器、MESSAGE CONTENT INTENT "
            "已开启，且在监听频道发过新帖。",
        )

    if newest_age is not None and newest_age > 3600:
        hints.append(
            "最近一条消息已超过 1 小时，可能没有新推文同步或存档任务未收到事件。",
        )

    return hints


@router.get("/api/integration/status", response_model=IntegrationEnvelope)
def integration_status(
    session: Session = Depends(db_session_dep),
    symbol: str = Query(default="SPY", min_length=1, max_length=12),
    settings: Settings = Depends(get_settings),
) -> IntegrationEnvelope:
    if not settings.integration_status_public:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail="Integration status endpoint disabled.",
        )

    chan_ids_sorted = sorted(parse_channel_ids(settings.discord_channel_ids))

    total_stmt = select(func.count()).select_from(DiscordMessageRow)
    total_rows = session.scalar(total_stmt)
    stored_total = int(total_rows or 0)

    recent_stmt = (
        select(DiscordMessageRow)
        .order_by(DiscordMessageRow.timestamp.desc())
        .limit(5)
    )
    recent_entities = list(session.scalars(recent_stmt).all())

    newest_age_seconds: Optional[float] = None
    if recent_entities:
        top_t = recent_entities[0].timestamp
        ts = (
            top_t.replace(tzinfo=timezone.utc)
            if getattr(top_t, "tzinfo", None) is None
            else top_t.astimezone(timezone.utc)
        )
        newest_age_seconds = max(
            0.0,
            (datetime.now(timezone.utc) - ts).total_seconds(),
        )

    previews: list[DiscordPreview] = []
    for row in recent_entities:
        raw_body = row.content or ""
        snippet = raw_body.strip().replace("\n", " ")
        snippets = snippet if len(snippet) <= 200 else snippet[:200] + "…"

        previews.append(
            DiscordPreview(
                id=row.id,
                channel_id=row.channel_id,
                author=row.author,
                timestamp=(
                    row.timestamp.astimezone(timezone.utc).isoformat()
                    if row.timestamp.tzinfo
                    else row.timestamp.replace(tzinfo=timezone.utc).isoformat()
                ),
                tickers=list(row.tickers or []),
                content_preview=snippets,
            ),
        )

    discord_block = DiscordIngest(
        discord_listener_setting=settings.enable_discord_listener,
        token_present=bool(settings.discord_bot_token.strip()),
        channel_ids=chan_ids_sorted,
        channel_ids_count=len(chan_ids_sorted),
        newest_message_age_seconds=newest_age_seconds,
        stored_message_count_total=stored_total,
        recent_preview=previews,
        hints=_preview_hints(cfg=settings, total=stored_total, newest_age=newest_age_seconds),
    )

    toolkit: OpenBBToolkit = build_default_toolkit()
    sym = symbol.strip().upper()
    quote_snapshot = toolkit.get_quote(sym)
    chain_snapshot = toolkit.get_option_chain(sym)
    trimmed_calls = chain_snapshot.get("calls_trimmed")
    trimmed_puts = chain_snapshot.get("puts_trimmed")
    options_block = OptionsProbe(
        symbol=sym,
        quote=quote_snapshot,
        option_chain_summary={
            "expiry": chain_snapshot.get("expiry"),
            "error": chain_snapshot.get("error"),
            "calls_rows": len(trimmed_calls)
            if isinstance(trimmed_calls, list)
            else None,
            "puts_rows": len(trimmed_puts) if isinstance(trimmed_puts, list) else None,
            "provider_note": (
                "OptionsAji Phase1 期权链经由 yfinance 获取；后续可切换 OpenBB Platform SDK。"
            ),
        },
    )

    envelope = IntegrationEnvelope(
        generated_at_utc=datetime.now(timezone.utc).isoformat(),
        discord=discord_block,
        options_via_yfinance=options_block,
    )
    return envelope
