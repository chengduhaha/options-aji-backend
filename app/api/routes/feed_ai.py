"""On-demand Feed item interpretations via OpenRouter (cached in Redis)."""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import bearer_subscription_optional
from app.config import get_settings
from app.services.cache_service import TTL_AI, cache_get, cache_set

logger = logging.getLogger(__name__)

router = APIRouter(tags=["feed"])


class FeedInterpretIn(BaseModel):
    id: str = Field(min_length=1, max_length=240)
    kind: str = Field(default="", max_length=32)
    title: str = Field(default="", max_length=520)
    body: str = Field(default="", max_length=4000)
    tickers: list[str] = Field(default_factory=list)


class InterpretBatchPayload(BaseModel):
    items: list[FeedInterpretIn] = Field(min_length=1, max_length=10)


class InterpretBatchEnvelope(BaseModel):
    interpretations: dict[str, str]
    cached: Optional[int] = None
    model: Optional[str] = None
    skipped: Optional[str] = None


def _item_cache_key(title: str, body: str) -> str:
    h = hashlib.sha256(f"{title}\n{body}".encode()).hexdigest()[:48]
    return f"feed:interp:v1:{h}"


def _call_batch_llm(prompt_block: str) -> dict[str, str]:
    cfg = get_settings()
    key = cfg.openrouter_api_key.strip()
    if not key:
        raise HTTPException(status_code=503, detail="openrouter_not_configured")

    model = cfg.model_synthesis.strip() or "deepseek/deepseek-chat"
    sys_msg = (
        "你是华语美股期权信息流编辑。给定若干条信息流片段（每条有 id）。"
        "请为每条写出 2~3 句中文解读：可能影响的标的或波动环境、粗略多空倾向（若非期权相关则说明信息性质）、"
        "一句风险提示。"
        '只输出合法 JSON：{"items":[{"id":"...","interpretation_zh":"..."}]}，数组顺序不限，'
        "不得捏造具体价位或保证收益。"
    )
    payload: dict[str, object] = {
        "model": model,
        "temperature": 0.25,
        "max_tokens": 2048,
        "messages": [
            {"role": "system", "content": sys_msg},
            {"role": "user", "content": prompt_block[:12000]},
        ],
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    url = f"{cfg.openrouter_base_url.rstrip('/')}/chat/completions"
    with httpx.Client(timeout=120.0) as client:
        resp = client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise HTTPException(status_code=502, detail="empty_llm_response")
    msg = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(msg, dict):
        raise HTTPException(status_code=502, detail="malformed_llm_response")
    content = msg.get("content")
    if not isinstance(content, str):
        raise HTTPException(status_code=502, detail="no_llm_content")
    try:
        raw = json.loads(content)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail="llm_json_parse_failed") from exc
    arr = raw.get("items") if isinstance(raw, dict) else None
    if not isinstance(arr, list):
        raise HTTPException(status_code=502, detail="llm_shape_invalid")

    out: dict[str, str] = {}
    for ent in arr:
        if not isinstance(ent, dict):
            continue
        eid = ent.get("id")
        zh = ent.get("interpretation_zh") if isinstance(ent.get("interpretation_zh"), str) else ""
        if not zh.strip():
            z2 = ent.get("zh")
            zh = z2 if isinstance(z2, str) else ""
        if isinstance(eid, str) and isinstance(zh, str) and eid.strip() and zh.strip():
            out[eid.strip()] = zh.strip()
    return out


@router.post("/api/feed/interpret-batch", response_model=InterpretBatchEnvelope)
def interpret_feed_batch(
    body: InterpretBatchPayload,
    _: Optional[str] = Depends(bearer_subscription_optional),
) -> InterpretBatchEnvelope:
    """Interpret up to 10 unified-feed rows per request; Redis TTL_AI cache."""

    results: dict[str, str] = {}
    needing: list[FeedInterpretIn] = []
    cached_n = 0

    for it in body.items:
        ck = _item_cache_key(it.title.strip(), it.body.strip())
        hit = cache_get(ck)
        if isinstance(hit, dict) and isinstance(hit.get("zh"), str) and hit["zh"].strip():
            results[it.id] = str(hit["zh"]).strip()
            cached_n += 1
            continue
        needing.append(it)

    if not needing:
        return InterpretBatchEnvelope(
            interpretations=results,
            cached=cached_n,
            model=None,
            skipped="all_cached",
        )

    prompt_lines: list[str] = []
    for idx, item in enumerate(needing):
        tick = ",".join(item.tickers[:12])[:200]
        b = item.body.strip()[:1200]
        prompt_lines.append(
            f"[{idx}] id={item.id}\nkind={item.kind}\ntickers={tick}\ntitle={item.title.strip()[:400]}\nbody={b}\n---",
        )
    merged = "\n".join(prompt_lines)

    mapped = _call_batch_llm(merged)
    cfg = get_settings()
    for it in needing:
        zh = mapped.get(it.id, "").strip()
        if zh:
            results[it.id] = zh
            ck = _item_cache_key(it.title.strip(), it.body.strip())
            cache_set(ck, {"zh": zh}, ttl=TTL_AI)

    return InterpretBatchEnvelope(
        interpretations=results,
        cached=cached_n,
        model=(cfg.model_synthesis.strip() or "deepseek/deepseek-chat"),
        skipped=None,
    )
