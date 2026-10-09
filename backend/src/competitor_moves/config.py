from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_CRON = "0 2 * * *"  # every site is crawled daily at 02:00 UTC unless its owner picks another schedule


class Settings(BaseSettings):
    """All configuration comes from environment variables (or a local .env)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    jwt_secret: str = Field(min_length=32)
    app_encryption_key: str = ""  # Fernet key; needed once a Magento token is stored
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash-lite"
    gemini_model_researcher: str = ""  # per-node overrides; empty = gemini_model
    gemini_model_summarize: str = ""
    gemini_model_analyst: str = ""
    gemini_model_extract: str = ""
    crawl_time_limit_sec: int = 600    # a site crawl stops (status partial) after this long
    product_page_meta: bool = True     # feed sites: also open each product page once for its meta tags
    meta_refresh_days: int = 7         # ...and again after this many days
    # pages built by JavaScript: rendered in headless Chrome (every request it makes passes the SSRF guard)
    browser: str = "auto"              # default per site: "auto" renders pages that need it, "off" never does
    browser_pages: int = 30            # most pages rendered per site per run (each counts as a page and a credit)
    browser_channel: str = ""          # "" = Playwright's bundled Chromium, "chrome" = the installed Google Chrome
    browser_timeout_sec: int = 30
    # Chromium on another machine (e.g. the user's own computer, by IP): a Playwright browser server's websocket,
    # ws://<host>:3000/<BROWSER_SERVER_SECRET> (the `browser` image). Empty = launch Chromium in this process.
    browser_ws_url: str = ""
    # pages with no structured data and no clear price: Gemini reads the visible text (prices it reads are tagged 'ai')
    ai_extract: bool = True
    ai_extract_cap: int = 30
    cors_origins: str = "*"
    public_dir: str = ""               # the UI's public/ folder; empty = <repo>/public next to backend/
    crawl_now_wait_sec: int = 90       # "Crawl now" waits this long for the crawls before answering
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
