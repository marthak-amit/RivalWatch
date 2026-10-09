from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration comes from environment variables (or a local .env)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    jwt_secret: str = Field(min_length=32)
    app_encryption_key: str = ""  # Fernet key; needed once a Magento token is stored
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash-lite"
    cors_origins: str = "*"
    idle_timeout_user_min: int = 480   # a user session ends after 8h without activity
    idle_timeout_admin_min: int = 30   # an admin session ends after 30 min without activity
    # Per-website limits live on the sites table; these are the defaults and the upper bound.
    default_max_products: int = 200  # products collected per site per run when the site has no setting
    max_products_cap: int = 5000     # nobody can ask for more than this per site
    page_budget: int = 400           # default cap on HTTP fetches per site per run
    max_agent_steps: int = 8
    user_agent: str = "RivalWatchBot/1.0 (+https://github.com/marthak-amit/RivalWatch)"
    crawl_delay_sec: float = 0.5     # pause between requests to the same site (robots.txt Crawl-delay wins if larger)
    fetch_timeout_sec: float = 20


@lru_cache
def get_settings() -> Settings:
    return Settings()
