from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "OptionsAji Backend"

    discord_bot_token: str = ""
    discord_channel_ids: str = ""
    discord_event_channel_ids: str = ""

    enable_discord_listener: bool = True

    #: Periodically fetch messages newer than DB max id per channel (REST `after`) to backfill gateway gaps.
    discord_gap_sync_enabled: bool = True
    discord_gap_sync_seconds: int = 300

    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    model_parse: str = "deepseek/deepseek-chat"
    model_synthesis: str = "deepseek/deepseek-chat"
    #: Empty → use ``model_synthesis`` for Discord feed LLM enrichment.
    feed_enrichment_model: str = ""

    openbb_api_key: str = ""
    fmp_api_key: str = ""

    subscription_tokens: str = ""
    subscription_required: bool = False

    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_id_pro: str = ""
    stripe_success_url: str = "http://localhost:3000/settings?billing=success"
    stripe_cancel_url: str = "http://localhost:3000/settings?billing=cancel"
    stripe_portal_return_url: str = "http://localhost:3000/settings?billing=portal"
    free_tier_daily_agent_queries: int = 20

    twitter_api_io_key: str = ""
    macro_feed_cache_seconds: int = 900

    database_url: str = "sqlite:///./data/messages.db"

    gex_backend_url: str = ""
    gex_backend_headers: str = ""

    retention_days: int = 3

    agent_discord_context_hours: int = 72
    agent_discord_context_limit: int = 18

    integration_status_public: bool = True

    admin_backfill_token: str = ""

    cors_origins: str = "*"

    cboe_equity_pc_csv_url: str = (
        "https://cdn.cboe.com/resources/options/volume_and_call_put_ratios/equitypc.csv"
    )
    cboe_equity_pc_cache_seconds: int = 3600

    #: When True, drop known-harmless ERROR/WARNING lines from yfinance/discord (Yahoo 404s, voice deps).
    suppress_noisy_provider_logs: bool = True

    #: Discord messages → Chinese insight via OpenRouter (requires API key).
    feed_enrichment_enabled: bool = True
    feed_enrichment_interval_seconds: int = 90
    feed_enrichment_batch_size: int = 8
    feed_enrichment_max_age_hours: int = 72


def get_settings() -> Settings:
    return Settings()
