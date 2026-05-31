"""LLM insight generation for supply-chain graph snapshots."""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from app.config import get_settings
from app.services.cache_service import TTL_WARM, cache_get, cache_set
from app.services.llm_router import build_chat_openai, has_llm_provider

logger = logging.getLogger(__name__)


def _cache_key(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return f"graph:insight:{digest}"


def _trim_payload(payload: dict[str, Any]) -> dict[str, Any]:
    nodes = payload.get("nodes")
    edges = payload.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise ValueError("nodes and edges are required")

    def trim_text(value: Any, max_len: int) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        return text if len(text) <= max_len else f"{text[:max_len]}…"

    return {
        "focus": str(payload.get("focus") or "").strip(),
        "perspective": str(payload.get("perspective") or "company").strip(),
        "depth": int(payload.get("depth") or 2),
        "asOf": payload.get("asOf"),
        "nodes": nodes[:80],
        "edges": [
            {
                **edge,
                "semantic": trim_text(edge.get("semantic") if isinstance(edge, dict) else None, 120),
            }
            if isinstance(edge, dict)
            else edge
            for edge in edges[:120]
        ],
    }


def generate_graph_insight(payload: dict[str, Any]) -> dict[str, object]:
    """Return cached or freshly generated graph insight text."""
    normalized = _trim_payload(payload)
    cache_key = _cache_key(normalized)
    cached = cache_get(cache_key)
    if isinstance(cached, dict) and cached.get("insight"):
        cached["cache"] = "hit"
        return cached

    cfg = get_settings()
    if not has_llm_provider(cfg):
        return {
            "insight": None,
            "error": "服务端未配置 LLM Provider，无法生成图谱解读。",
            "cache": "miss",
        }

    llm = build_chat_openai(
        cfg,
        openrouter_model=cfg.model_synthesis,
        source="graph_insight",
        temperature=0.35,
        timeout=90,
        max_retries=1,
    )

    system = (
        "你是 OptionsAji 的产业星图分析师。根据用户提供的供应链/投资关系子图 JSON，"
        "用中文写 3–5 段解读，面向华语美股投资者。\n"
        "结构建议：\n"
        "1. 格局总览（焦点公司处于什么位置）\n"
        "2. 分部/行业与供应链结构\n"
        "3. 护城河与关键依赖（结合 moatTier、supplies_to、invests_in）\n"
        "4. 风险与跟踪要点\n"
        "要求：简洁、具体、引用 JSON 中的公司名/关系 label；不要编造未出现的实体；"
        "结尾一句免责声明：不构成投资建议。"
    )
    human = HumanMessage(
        content=json.dumps(normalized, ensure_ascii=False, default=str),
    )

    try:
        out = llm.invoke([SystemMessage(content=system), human])
        text = getattr(out, "content", None)
        insight = "" if text is None else str(text).strip()
    except Exception as exc:
        logger.exception("Graph insight generation failed: %s", exc)
        return {
            "insight": None,
            "error": "生成图谱解读失败，请稍后重试。",
            "cache": "miss",
        }

    result = {"insight": insight, "cache": "miss"}
    cache_set(cache_key, result, ttl=TTL_WARM)
    return result
