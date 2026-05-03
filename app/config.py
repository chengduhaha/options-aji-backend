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

    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    model_parse: str = "deepseek/deepseek-chat"
    model_synthesis: str = "deepseek/deepseek-chat"

    openbb_api_key: str = ""

    subscription_tokens: str = ""
    subscription_required: bool = False

    database_url: str = "sqlite:///./data/messages.db"

    gex_backend_url: str = ""
    gex_backend_headers: str = ""

    retention_days: int = 3

    agent_discord_context_hours: int = 72
    agent_discord_context_limit: int = 18

    integration_status_public: bool = True

    admin_backfill_token: str = ""

    cors_origins: str = "*"


def get_settings() -> Settings:
    return Settings()
