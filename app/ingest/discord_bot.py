"""Listen to Discord gateways and persist channel messages."""

from __future__ import annotations

import asyncio
import logging

import discord

from app.config import get_settings
from app.db.session import SessionLocal
from app.ingest.message_store import upsert_discord_row
from app.ingest.tickers import extract_tickers

logger = logging.getLogger(__name__)


def parse_channel_ids(raw: str) -> set[str]:
    return {p.strip() for p in raw.split(",") if p.strip()}


def build_client(*, allowed_channels: set[str]) -> discord.Client:
    intents = discord.Intents.default()
    intents.message_content = True
    intents.guild_messages = True

    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        uid = getattr(client.user, "id", "?")
        display = getattr(client.user, "name", "")
        logger.info("Discord ingest online as %s (#%s)", display, uid)

    @client.event
    async def on_message(message: discord.Message) -> None:
        if getattr(message.author, "bot", False):
            return
        ch_id = str(message.channel.id)
        if ch_id not in allowed_channels:
            return

        tickers = extract_tickers(message.content or "")
        author = getattr(message.author, "global_name", None) or message.author.name

        ts = message.created_at
        cid = str(message.channel.id)

        def persist() -> None:
            with SessionLocal() as session:
                upsert_discord_row(
                    session,
                    message_id=str(message.id),
                    channel_id=cid,
                    author=author,
                    content=(message.content or "")[:8192],
                    when=ts,
                    tickers=tickers,
                )

        try:
            await asyncio.to_thread(persist)
        except Exception:
            logger.exception("Failed to ingest discord message %s", message.id)

    return client


async def run_discord_ingest_forever() -> None:
    """Blocks until discord logs out."""

    cfg = get_settings()
    tok = cfg.discord_bot_token.strip()
    channels = parse_channel_ids(cfg.discord_channel_ids)

    if not tok or not channels:
        logger.warning(
            "Discord ingest inactive: configure DISCORD_BOT_TOKEN and DISCORD_CHANNEL_IDS",
        )
        return

    logger.info(
        "Discord ingest starting for %s channel ids (example first=%s)",
        len(channels),
        next(iter(channels)),
    )
    bot = build_client(allowed_channels=channels)
    await bot.start(tok)
