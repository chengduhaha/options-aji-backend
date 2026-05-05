"""Application settings — loaded from .env file."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "OptionsAji Backend"
    log_level: str = "INFO"

    # ── Database ──────────────────────────────────────────────────────────────
    database_url: str = "sqlite:///./data/messages.db"

    # ── Redis cache ───────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"
    redis_cache_ttl_hot: int = 900     # 15 min — options snapshots, quotes
    redis_cache_ttl_warm: int = 1800   # 30 min — news, analyst ratings
    redis_cache_ttl_cold: int = 21600  # 6 hrs — financials, ETF holdings
    redis_cache_ttl_ai: int = 3600     # 1 hr  — AI summaries

    # ── Massive API (options) ─────────────────────────────────────────────────
    massive_api_key: str = ""
    massive_base_url: str = "https://api.massive.com"
    massive_ws_url: str = "wss://delayed.massive.com/options"

    # ── FMP API (stocks / financials) ─────────────────────────────────────────
    fmp_api_key: str = ""
    fmp_base_url: str = "https://financialmodelingprep.com/stable"

    # ── OpenRouter LLM ────────────────────────────────────────────────────────
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    model_parse: str = "deepseek/deepseek-chat"
    model_synthesis: str = "deepseek/deepseek-chat"
    feed_enrichment_model: str = ""

    # ── Data sync ─────────────────────────────────────────────────────────────
    # Comma-separated list of symbols to keep in sync
    sync_watchlist: str = "SPY,QQQ,AAPL,MSFT,NVDA,TSLA,AMZN,META,GOOGL,AMD"
    sync_enabled: bool = True
    sync_timezone: str = "America/New_York"

    # ── Discord (optional) ────────────────────────────────────────────────────
    discord_bot_token: str = ""
    discord_channel_ids: str = ""
    discord_event_channel_ids: str = ""
    enable_discord_listener: bool = False
    discord_gap_sync_enabled: bool = False
    discord_gap_sync_seconds: int = 300

    # ── Feed enrichment ───────────────────────────────────────────────────────
    feed_enrichment_enabled: bool = True
    feed_enrichment_interval_seconds: int = 90
    feed_enrichment_batch_size: int = 8
    feed_enrichment_max_age_hours: int = 72

    # ── Stripe billing ────────────────────────────────────────────────────────
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_id_pro: str = ""
    stripe_success_url: str = "http://localhost:3000/settings?billing=success"
    stripe_cancel_url: str = "http://localhost:3000/settings?billing=cancel"
    stripe_portal_return_url: str = "http://localhost:3000/settings?billing=portal"
    free_tier_daily_agent_queries: int = 20

    # ── Access control ────────────────────────────────────────────────────────
    subscription_tokens: str = ""
    subscription_required: bool = False
    admin_backfill_token: str = ""

    # ── CORS ─────────────────────────────────────────────────────────────────
    cors_origins: str = "*"

    # ── Misc ─────────────────────────────────────────────────────────────────
    openbb_api_key: str = ""
    twitter_api_io_key: str = ""
    macro_feed_cache_seconds: int = 900
    cboe_equity_pc_cache_seconds: int = 3600
    cboe_equity_pc_csv_url: str = (
        "https://cdn.cboe.com/resources/options/volume_and_call_put_ratios/equitypc.csv"
    )
    gex_backend_url: str = ""
    gex_backend_headers: str = ""
    retention_days: int = 3
    agent_discord_context_hours: int = 72
    agent_discord_context_limit: int = 18
    integration_status_public: bool = True
    suppress_noisy_provider_logs: bool = True

    @property
    def sync_watchlist_symbols(self) -> list[str]:
        return [s.strip().upper() for s in self.sync_watchlist.split(",") if s.strip()]

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")

    @property
    def redis_enabled(self) -> bool:
        return bool(self.redis_url.strip())


def get_settings() -> Settings:
    return Settings()
