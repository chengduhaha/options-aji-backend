"""Translate Polymarket event titles to Chinese with Redis cache."""
from __future__ import annotations

import logging
import re

from app.config import get_settings
from app.services.cache_service import cache_get, cache_set
from app.services.llm_router import has_llm_provider, post_chat_completions_with_fallback

logger = logging.getLogger(__name__)

_CACHE_PREFIX = "pm:title_zh:"
_TITLE_TTL = 604800  # 7 days


def _has_chinese(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text))


def _cache_key(event_id: str) -> str:
    return f"{_CACHE_PREFIX}{event_id}"


def _llm_translate(title_en: str) -> str | None:
    cfg = get_settings()
    if not has_llm_provider(cfg):
        return None
    payload: dict[str, object] = {
        "temperature": 0.1,
        "max_tokens": 120,
        "messages": [
            {
                "role": "system",
                "content": (
                    "将 Polymarket 预测市场英文标题译为简洁中文（≤50字），面向中国美股投资者。"
                    "只输出译文，不要引号、编号或解释。"
                ),
            },
            {"role": "user", "content": title_en[:500]},
        ],
    }
    try:
        data, _provider = post_chat_completions_with_fallback(
            payload,
            cfg=cfg,
            source="polymarket_title",
            timeout=45.0,
        )
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return None
        first = choices[0]
        if not isinstance(first, dict):
            return None
        msg = first.get("message")
        if not isinstance(msg, dict):
            return None
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()[:512]
    except Exception as exc:
        logger.warning("polymarket title translation failed: %s", exc)
    return None


def resolve_title_zh(event_id: str, title_en: str) -> str:
    """Return Chinese title; cache LLM output when available."""
    clean_en = title_en.strip()
    if not clean_en:
        return "未知事件"
    if _has_chinese(clean_en):
        return clean_en

    cached = cache_get(_cache_key(event_id))
    if isinstance(cached, str) and cached.strip():
        return cached.strip()

    translated = _llm_translate(clean_en)
    if translated:
        cache_set(_cache_key(event_id), translated, ttl=_TITLE_TTL)
        return translated

    return clean_en
