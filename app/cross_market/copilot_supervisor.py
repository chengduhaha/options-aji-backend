"""Copilot agent — DeepAgents + ontology tools."""
from __future__ import annotations

import logging
import os
from typing import Any

from app.cross_market.actions import ALL_ACTIONS
from app.cross_market.ontology_registry import ontology
from app.services.llm_router import build_chat_openai, has_llm_provider

try:
    from deepagents import create_deep_agent
except ModuleNotFoundError:
    create_deep_agent = None

logger = logging.getLogger(__name__)


class FallbackAgent:
    """Fallback when deepagents is unavailable."""

    def __init__(self, model: ChatOpenAI, instructions: str):
        self.model = model
        self.instructions = instructions

    async def ainvoke(self, payload: dict[str, Any]) -> dict[str, Any]:
        messages = payload.get("messages", [])
        user_text = ""
        if messages:
            user_text = str(messages[-1].get("content", ""))
        prompt = (
            f"{self.instructions}\n\n"
            "你当前运行在 fallback 模式（deepagents 未安装），请输出结构化中文分析。\n"
            f"用户问题: {user_text}"
        )
        result = await self.model.ainvoke(prompt)
        return {"messages": [{"role": "assistant", "content": str(result.content)}]}

    async def astream_events(self, payload: dict[str, Any], version: str = "v2"):
        _ = version
        output = await self.ainvoke(payload)
        yield {"event": "on_chain_end", "data": output["messages"][-1]["content"]}


def build_supervisor():
    if not has_llm_provider():
        logger.warning("No LLM provider configured; Copilot will fail until configured")

    model = build_chat_openai(
        openrouter_model=os.getenv("COPILOT_MODEL", "").strip()
        or os.getenv("SUPERVISOR_MODEL", "").strip()
        or None,
        xiaomi_model=os.getenv("COPILOT_XIAOMI_MODEL", "").strip() or None,
        source="copilot",
        temperature=0.3,
    )

    available_patterns = ontology.list_patterns()
    pattern_descriptions = "\n".join(
        f"- {pattern_id}: {ontology.load_pattern(pattern_id).display_name_zh or pattern_id}"
        for pattern_id in available_patterns
    )
    available_objects = ontology.list_objects()

    instructions = f"""你是 OptionsAji 的 AI 期权操盘助理。你工作在一个 Ontology 上。

## 你可访问的 Ontology Objects
{", ".join(available_objects)}

## 你可使用的推理模板(Patterns)
{pattern_descriptions}

## 工作原则
1. 总是先列出计划
2. 优先使用 Pattern
3. 跨市场视角: options / polymarket / social / institutional
4. 始终中文回复,英文术语保留(IV, GEX, Delta 等)
5. 不直接给买卖指令,提供推理与风险
6. 美股数据：问实时股价、期权链、单合约 IV/Greeks、新闻标题时，优先使用 get_stock_quote（启用 IBKR 时走 Gateway）、get_options_landscape、get_option_contract_ibkr、get_option_chain_snapshot_tool_ibkr、get_symbol_news_ibkr
"""

    if create_deep_agent is None:
        return FallbackAgent(model=model, instructions=instructions)

    return create_deep_agent(model=model, tools=ALL_ACTIONS, system_prompt=instructions)
