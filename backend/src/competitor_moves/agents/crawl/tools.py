"""The six crawl tools as LLM tools. Docstrings are what the model reads (agent spec §1.6); the logic lives in
services/site_crawl.py so the fallback plan runs the same code. No tool accepts a domain or a free URL."""
from typing import Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from ...services import site_crawl as sc


def _ctx(config: RunnableConfig) -> sc.RunContext:
    return config["configurable"]["run"]


@tool
def inspect_site(config: RunnableConfig) -> str:
    """Look at the competitor's homepage and robots.txt. Reports the platform, whether a product feed exists,
    the sitemaps, the menu categories and any homepage promotions. Call this first, once per run."""
    return sc.inspect_site(_ctx(config))


@tool
def fetch_product_feed(config: RunnableConfig, max_items: int | None = None) -> str:
    """Read products from the platform's own product feed (Shopify or WooCommerce). The fastest and most
    accurate source: about 250 products per page. Only works if inspect_site reported a product feed.
    max_items (optional) caps how many products to read; it never exceeds the run's product limit."""
    return sc.fetch_product_feed(_ctx(config), max_items)


@tool
def read_sitemap(config: RunnableConfig, changed_since_days: int | None = None) -> str:
    """Read the sitemap and queue product page URLs for fetch_queued_products. Does not fetch product pages.
    Use changed_since_days (1-365) on incremental runs to queue only recently changed products; leave it empty
    on a first run to queue all."""
    return sc.read_sitemap(_ctx(config), changed_since_days)


@tool
def fetch_category_listing(category: str, config: RunnableConfig, max_pages: int = 2) -> str:
    """Open a category from the site menu and queue the product links on its listing pages. Use only when there
    is no product feed and the sitemap has no product URLs. category must be copied exactly from inspect_site's
    menu list. max_pages is 1 to 5 listing pages."""
    return sc.fetch_category_listing(_ctx(config), category, max_pages)


@tool
def fetch_queued_products(source: Literal["recent", "all", "sample"], config: RunnableConfig,
                          limit: int | None = None) -> str:
    """Fetch product pages from a queue built by read_sitemap or fetch_category_listing and store their name,
    price, stock and details. 'recent' = changed since the last run, 'all' = everything queued, 'sample' = random
    products not stored yet. limit (optional) caps how many pages to fetch."""
    return sc.fetch_queued_products(_ctx(config), source, limit)


@tool
def recheck_known_products(config: RunnableConfig, limit: int | None = None) -> str:
    """Re-fetch products already stored for this site, least recently checked first, to catch price and stock
    changes. Use on incremental runs when there is no product feed. limit (optional) caps how many to recheck."""
    return sc.recheck_known_products(_ctx(config), limit)


TOOLS = [inspect_site, fetch_product_feed, read_sitemap, fetch_category_listing, fetch_queued_products,
         recheck_known_products]
TOOLS_BY_NAME = {t.name: t for t in TOOLS}
