"""Fallback LLM agent when deepagents is unavailable."""
from __future__ import annotations

from typing import Any


class FallbackAgent:
    """Minimal async agent wrapping a chat model with a static system prompt."""

    def __init__(self, model: object, instructions: str) -> None:
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
