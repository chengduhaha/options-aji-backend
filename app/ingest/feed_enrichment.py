"""Background OpenRouter pipeline: Discord messages → Chinese insight JSON."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
from typing import Callable, Optional

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import DiscordMessageRow, MessageEnrichmentRow
from app.db.session import SessionLocal

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], Session]

_MAX_INPUT_CHARS = 6000


class _EnrichmentLLMOut(BaseModel):
    language_detected: Optional[str] = None
    title_zh: str = ""
    summary_zh: str = ""
    bullets_zh: list[str] = Field(default_factory=list)
    risk_note_zh: Optional[str] = None


def _enrichment_model_id(cfg: Settings) -> str:
    custom = cfg.feed_enrichment_model.strip()
    return custom if custom else cfg.model_synthesis


def _strip_json_fence(raw: str) -> str:
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


def _call_openrouter_enrich(
    cfg: Settings,
    *,
    plaintext: str,
    author: Optional[str],
) -> _EnrichmentLLMOut | None:
    key = cfg.openrouter_api_key.strip()
    if not key:
        return None

    sys_prompt = (
        "你是面向华语期权交易者的信息流编辑。用户将提供一条 Discord 存档原文。"
        "请只输出一个 JSON 对象，键为："
        'language_detected（ISO 639-1 如 en/zh，猜不出用 unknown）、'
        "title_zh（≤40 字中文标题）、summary_zh（2~4 句通俗中文，解释对交易者的含义）、"
        "bullets_zh（字符串数组，恰好 3 条短句，递进）、"
        "risk_note_zh（一句风险提示，无可写“信息有限，请交叉验证”）。"
        "禁止编造具体价格、点位、保证收益；不得输出 JSON 外文字。"
    )
    user_block = f"作者: {author or 'unknown'}\n\n原文:\n{plaintext[:_MAX_INPUT_CHARS]}"
    payload: dict[str, object] = {
        "model": _enrichment_model_id(cfg),
        "temperature": 0.2,
        "max_tokens": 1024,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_block},
        ],
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    url = f"{cfg.openrouter_base_url.rstrip('/')}/chat/completions"
    try:
        with httpx.Client(timeout=120.0) as client:
            resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
        logger.exception("OpenRouter enrichment HTTP error: %s", exc)
        return None

    try:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return None
        msg = choices[0].get("message") if isinstance(choices[0], dict) else None
        if not isinstance(msg, dict):
            return None
        content = msg.get("content")
        if not isinstance(content, str) or not content.strip():
            return None
        parsed = json.loads(_strip_json_fence(content))
        if not isinstance(parsed, dict):
            return None
        return _EnrichmentLLMOut.model_validate(parsed)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Enrichment JSON parse failed: %s", exc)
        return None


def process_pending_enrichments(
    session_factory: SessionFactory,
    *,
    batch_size: int,
    max_age_hours: int,
) -> int:
    cfg = get_settings()
    if not cfg.feed_enrichment_enabled:
        return 0
    if not cfg.openrouter_api_key.strip():
        return 0

    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        hours=max(1, int(max_age_hours)),
    )
    done = 0
    session = session_factory()
    try:
        pending_stmt = (
            select(DiscordMessageRow)
            .where(
                ~exists(
                    select(1).where(
                        MessageEnrichmentRow.message_id == DiscordMessageRow.id,
                    ),
                ),
                DiscordMessageRow.content.isnot(None),
                DiscordMessageRow.timestamp >= since,
            )
            .order_by(DiscordMessageRow.timestamp.desc())
            .limit(max(1, int(batch_size)))
        )
        rows = list(session.scalars(pending_stmt).all())
    finally:
        session.close()

    model_id = _enrichment_model_id(cfg)
    for row in rows:
        plain = (row.content or "").strip()
        if not plain:
            continue
        parsed = _call_openrouter_enrich(cfg, plaintext=plain, author=row.author)
        if parsed is None:
            continue

        sess = session_factory()
        try:
            bullets = [str(b).strip() for b in parsed.bullets_zh if str(b).strip()][:5]
            enr = MessageEnrichmentRow(
                message_id=row.id,
                language_detected=parsed.language_detected,
                title_zh=(parsed.title_zh or "")[:512] or None,
                summary_zh=parsed.summary_zh or None,
                bullets_zh=bullets,
                risk_note_zh=(parsed.risk_note_zh or None),
                model=model_id,
            )
            sess.add(enr)
            dm = sess.get(DiscordMessageRow, row.id)
            if dm is not None:
                dm.processed = True
            sess.commit()
            done += 1
        except Exception:
            logger.exception("Persist enrichment failed message_id=%s", row.id)
            sess.rollback()
        finally:
            sess.close()

    if done:
        logger.info("Feed enrichment batch: processed=%s", done)
    return done


async def run_feed_enrichment_loop() -> None:
    lock = asyncio.Lock()
    while True:
        cfg = get_settings()
        interval = max(30, int(cfg.feed_enrichment_interval_seconds))
        await asyncio.sleep(float(interval))
        if not cfg.feed_enrichment_enabled:
            continue
        if not cfg.openrouter_api_key.strip():
            continue

        batch = max(1, int(cfg.feed_enrichment_batch_size))
        max_age = max(1, int(cfg.feed_enrichment_max_age_hours))

        async with lock:

            def _work() -> int:
                return process_pending_enrichments(
                    SessionLocal,
                    batch_size=batch,
                    max_age_hours=max_age,
                )

            await asyncio.to_thread(_work)
