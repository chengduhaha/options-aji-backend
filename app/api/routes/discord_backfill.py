"""One-shot Discord REST backfill into local SQLite."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.db.session import SessionLocal
from app.ingest.discord_history_rest import backfill_configured_channels

router = APIRouter(tags=["integration"])


class DiscordBackfillRequest(BaseModel):
    days: float = Field(default=3.0, ge=0.25, le=14.0, description="How far back to walk history")
    channel_ids: Optional[list[str]] = Field(
        default=None,
        description="Override comma-list from DISCORD_CHANNEL_IDS; defaults to configured channels.",
    )
    include_bots: bool = Field(
        default=True,
        description="Include bot/webhook relays (recommended for TweetShift archives).",
    )


def _guard_admin(secret_header: Optional[str], settings: Settings) -> None:
    expected = settings.admin_backfill_token.strip()
    if not expected:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ADMIN_BACKFILL_TOKEN unset — backfill POST disabled.",
        )
    incoming = (secret_header or "").strip()
    if incoming != expected:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid admin token.")


@router.post("/api/integration/discord/backfill")
def trigger_discord_backfill(
    body: DiscordBackfillRequest,
    settings: Settings = Depends(get_settings),
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, object]:
    """
    Pull historical Discord channel messages via REST and upsert SQLite rows.

    Requires `ADMIN_BACKFILL_TOKEN` in backend env plus matching `X-Admin-Token` header.
    """

    _guard_admin(x_admin_token, settings)

    tok = settings.discord_bot_token.strip()
    channels_csv = settings.discord_channel_ids.strip()

    outcome = backfill_configured_channels(
        session_factory=SessionLocal,
        token=tok,
        channel_csv=channels_csv,
        days=float(body.days),
        include_bots=body.include_bots,
        channel_override=body.channel_ids,
    )

    outcome["requested_days"] = body.days
    outcome["channels_override"] = bool(body.channel_ids)
    outcome["hint"] = (
        "若仍看不到历史，请核对 DISCORD_CHANNEL_IDS 是否包含「实时美股消息区」频道 ID。"
    )
    return outcome
