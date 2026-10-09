"""Which shop platform a site runs on, from its homepage HTML."""


def detect(html: str) -> str:
    h = html[:500_000].lower()
    if "cdn.shopify.com" in h or "shopify.theme" in h or ".myshopify.com" in h:
        return "shopify"
    if "woocommerce" in h:
        return "woocommerce"
    if "magento_" in h or "mage/cookies" in h or "/static/version" in h or "x-magento-init" in h:
        return "magento"
    return "generic"


def feed_url(platform: str, base: str, page: int) -> str | None:
    """URL of one page of the platform's public product feed, or None if it has no such feed."""
    base = base.rstrip("/")
    if platform == "shopify":
        return f"{base}/products.json?limit=250&page={page}"
    if platform == "woocommerce":
        return f"{base}/wp-json/wc/store/v1/products?per_page=100&page={page}"
    return None
