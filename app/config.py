"""Application settings.

职责分工：

- **本文件 (config.py)**：声明有哪些配置项、类型与**非敏感默认值**。这是代码，应提交 Git；
  不应把真实密钥写死在源码里。

- **settings.toml**（可选，与 ``app/`` 同级）：推荐的「单一运维配置文件」，复制
  ``settings.example.toml`` 后填写；已在 ``.gitignore``，勿提交。

- **.env**（可选）：与 Docker / 旧部署兼容；字段名与环境变量一致（大写）。

加载优先级（同名键，先出现的生效——后面的来源无法覆盖）：

``构造函数参数 > 环境变量 > settings.toml > .env > 本文件中的默认值``

因此线上可用环境变量覆盖文件中的配置；本地可只维护 ``settings.toml``，不必同时维护两份密钥列表。
"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)
from pydantic_settings.sources import TomlConfigSettingsSource

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_DOTENV_PATH = _BACKEND_ROOT / ".env"
_SETTINGS_TOML_PATH = _BACKEND_ROOT / "settings.toml"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_DOTENV_PATH if _DOTENV_PATH.is_file() else None,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        sources: list[PydanticBaseSettingsSource] = [
            init_settings,
            env_settings,
        ]
        if _SETTINGS_TOML_PATH.is_file():
            sources.append(TomlConfigSettingsSource(settings_cls, _SETTINGS_TOML_PATH))
        sources.append(dotenv_settings)
        sources.append(file_secret_settings)
        return tuple(sources)

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

    # ── JWT user auth ───────────────────────────────────────────────────────────
    jwt_secret_key: str = ""
    jwt_expire_hours: int = 24
    # Comma-separated emails that receive role "admin" on registration.
    auth_admin_emails: str = ""
    auth_register_rate_limit_enabled: bool = True
    auth_register_max_per_hour: int = 10
    auth_register_window_seconds: int = 3600
    auth_verification_code_ttl_seconds: int = 900
    auth_verification_max_attempts: int = 5
    auth_verification_debug_expose_code: bool = False

    # ── Access control ────────────────────────────────────────────────────────
    subscription_tokens: str = ""
    subscription_required: bool = False
    admin_backfill_token: str = ""

    # ── CORS ─────────────────────────────────────────────────────────────────
    cors_origins: str = "*"
    cors_allow_credentials: bool = True

    # ── Misc ─────────────────────────────────────────────────────────────────
    openbb_api_key: str = ""
    twitter_api_io_key: str = ""
    xpoz_api_key: str = ""
    xpoz_base_url: str = "https://api.xpoz.ai/v1"
    xpoz_retry_max: int = 2
    xpoz_retry_backoff_seconds: float = 0.5
    xpoz_circuit_breaker_seconds: int = 120
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
    feature_social_enabled: bool = True
    #: Comma-separated Twitter/X handles (no @) for KOL timeline ingest via xpoz
    social_kol_handles: str = (
        "unusual_whales,OptionsHawk,SqueezeMetrics,CBOE,DeItaone,gurgavin"
    )
    feature_deep_agent_enabled: bool = True

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
