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


def _plaintext_from_discord_message(message: discord.Message) -> str:
    """Join user-visible text: body, embeds (TweetShift 等常只有 embed), attachments."""
    parts: list[str] = []
    content = (message.content or "").strip()
    if content:
        parts.append(content)
    for emb in message.embeds:
        block: list[str] = []
        title = getattr(emb, "title", None)
        if isinstance(title, str) and title.strip():
            block.append(title.strip())
        desc = getattr(emb, "description", None)
        if isinstance(desc, str) and desc.strip():
            block.append(desc.strip())
        url = getattr(emb, "url", None)
        if isinstance(url, str) and url.strip():
            block.append(url.strip())
        if block:
            parts.append("\n".join(block))
    if message.attachments:
        names = ", ".join((a.filename or "[file]") for a in message.attachments)
        parts.append(f"[附件] {names}")
    joined = "\n".join(p for p in parts if p).strip()
    return joined[:8192]


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
        ch_id = str(message.channel.id)
        if ch_id not in allowed_channels:
            return

        plaintext = _plaintext_from_discord_message(message)
        if not plaintext:
            return

        tickers = extract_tickers(plaintext)
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
                    content=plaintext,
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
