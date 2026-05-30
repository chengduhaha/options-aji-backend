from app.cross_market.actions.analysis_actions import (
    ontology_trace_impact_chain,
    probability_fusion,
)
from app.cross_market.actions.data_actions import (
    get_option_chain_params_ibkr,
    get_option_chain_snapshot_tool_ibkr,
    get_option_contract_ibkr,
    get_options_landscape,
    get_realtime_quote_ibkr,
    get_stock_quote,
    get_symbol_news_ibkr,
    polymarket_search_and_quote,
    sentiment_quantify,
)
from app.cross_market.actions.recommendation_actions import (
    design_multi_asset_portfolio,
    recommend_options_strike,
)

ALL_ACTIONS = [
    polymarket_search_and_quote,
    get_options_landscape,
    get_stock_quote,
    get_realtime_quote_ibkr,
    get_option_chain_params_ibkr,
    get_option_contract_ibkr,
    get_option_chain_snapshot_tool_ibkr,
    get_symbol_news_ibkr,
    sentiment_quantify,
    probability_fusion,
    ontology_trace_impact_chain,
    design_multi_asset_portfolio,
    recommend_options_strike,
]
