"""LangChain `StructuredTool` facades over :class:`OpenBBToolkit` (Phase 1 wiring)."""

from __future__ import annotations

import json

from typing import Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.tools.openbb_tools import OpenBBToolkit, build_default_toolkit


class SymbolArgs(BaseModel):
    symbol: str = Field(description="US equity ticker symbol, e.g. SPY")


def toolkit_to_langchain_tools(
    toolkit: Optional[OpenBBToolkit] = None,
) -> list[StructuredTool]:
    """Return deterministic OpenBB-aligned tools usable by LangGraph ReAct planners."""

    service = toolkit or build_default_toolkit()

    def _dump(payload: dict[str, object]) -> str:
        return json.dumps(payload, ensure_ascii=False, default=str)

    quote_tool = StructuredTool.from_function(
        name="openbb_quote",
        description="Fetch live quote snapshots (via yfinance bridge).",
        args_schema=SymbolArgs,
        func=lambda symbol: _dump(service.get_quote(symbol)),
        return_direct=False,
    )

    chain_tool = StructuredTool.from_function(
        name="openbb_option_chain_digest",
        description="Retrieve nearest-expiry option chain excerpts for context shaping.",
        args_schema=SymbolArgs,
        func=lambda symbol: _dump(service.get_option_chain(symbol)),
        return_direct=False,
    )

    gex_tool = StructuredTool.from_function(
        name="openbb_gamma_exposure",
        description=(
            "Net GEX / walls：优先 GEX_BACKEND_URL；否则本地 yfinance 估计。"
        ),
        args_schema=SymbolArgs,
        func=lambda symbol: _dump(service.get_gex(symbol)),
        return_direct=False,
    )

    return [quote_tool, chain_tool, gex_tool]
