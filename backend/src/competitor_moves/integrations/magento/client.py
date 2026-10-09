"""Our own Magento store: the catalog is read through GraphQL (GET queries, through the SSRF-guarded fetcher), prices
are read and changed through the price REST APIs (the only writes).

The catalog is public, so GraphQL never gets the token: Magento 2.4.4+ rejects an integration token sent as a Bearer to
GraphQL ("Composite reader could not read a token") unless the store allows it. The integration token is only for the
price APIs. `store_code` selects the store view.
"""
import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import urlencode

from ...crawler import attributes
from ...crawler.extract import to_decimal
from ...crawler.fetch import Page, fetch, send

PAGE_SIZE = 100
STORE_QUERY = "{ storeConfig { store_name base_currency_code base_url product_url_suffix } }"
PRODUCTS_QUERY = """query ($page: Int!, $size: Int!) { products(search: "", pageSize: $size, currentPage: $page) {
  total_count page_info { total_pages current_page }
  items { sku name url_key url_suffix created_at updated_at stock_status meta_title meta_description
          categories { name level } small_image { url }
          price_range { minimum_price { regular_price { value currency } final_price { value currency } } } } } }"""


class MagentoError(Exception):
    """The store answered with an error (message is safe to show)."""


class MagentoClient:
    def __init__(self, base_url: str, *, user_agent: str, store_code: str = "default", token: str | None = None,
                 timeout: float = 30):
        self.base = base_url.rstrip("/")
        self.headers, self.token = {"Store": store_code or "default"}, token
        self.user_agent, self.timeout, self.requests = user_agent, timeout, 0

    def _query(self, query: str, variables: dict | None = None) -> dict:
        params = {"query": " ".join(query.split())}
        if variables:
            params["variables"] = json.dumps(variables)
        page: Page = fetch(f"{self.base}/graphql?{urlencode(params)}", user_agent=self.user_agent, timeout=self.timeout,
                           accept="application/json", stay_on=self.base, headers=self.headers)
        self.requests += 1
        try:
            body = json.loads(page.body)
        except ValueError:
            raise MagentoError(f"not a Magento GraphQL endpoint (HTTP {page.status})") from None
        if body.get("errors"):
            raise MagentoError("; ".join(str(e.get("message")) for e in body["errors"])[:300])
        if page.status != 200 or not isinstance(body.get("data"), dict):
            raise MagentoError(f"HTTP {page.status}")
        return body["data"]

    def store(self) -> dict:
        return self._query(STORE_QUERY)["storeConfig"]

    def products(self, page: int, size: int = PAGE_SIZE) -> tuple[list[dict], int, int]:
        """(items, total_count, total_pages) for one page."""
        p = self._query(PRODUCTS_QUERY, {"page": page, "size": size})["products"]
        return p["items"] or [], p["total_count"], p["page_info"]["total_pages"]

    # ---- prices: Magento's price REST APIs (the token needs Catalog → Inventory → Products) -------------------------

    def _rest(self, path: str, body: dict):
        if not self.token:
            raise MagentoError("changing prices needs an integration token on the store connection")
        page = send(f"{self.base}/rest/V1/{path}", body, user_agent=self.user_agent, timeout=self.timeout,
                    headers={"Authorization": f"Bearer {self.token}"})
        self.requests += 1
        try:
            data = json.loads(page.body)
        except ValueError:
            raise MagentoError(f"not a Magento REST endpoint (HTTP {page.status})") from None
        if page.status != 200:
            raise MagentoError(f"HTTP {page.status}: {_message(data)}")
        return data

    def price_info(self, sku: str) -> tuple[Decimal | None, Decimal | None]:
        """(base price, cost) at the default scope (store_id 0); cost is None when the product has none."""
        def pick(rows, field):
            rows = rows if isinstance(rows, list) else []
            row = next((r for r in rows if r.get("store_id") == 0), rows[0] if rows else None)
            return to_decimal(row.get(field)) if row else None
        return (pick(self._rest("products/base-prices-information", {"skus": [sku]}), "price"),
                pick(self._rest("products/cost-information", {"skus": [sku]}), "cost"))

    def set_price(self, sku: str, price: Decimal) -> None:
        """Set the base (regular) price at the default scope. Only the price changes; a sale price stays."""
        errors = self._rest("products/base-prices", {"prices": [{"sku": sku, "price": float(price), "store_id": 0}]})
        if errors:
            raise MagentoError("; ".join(_message(e) for e in errors)[:300])


def _message(data) -> str:
    """Magento's error text with its %placeholders filled in."""
    if not isinstance(data, dict):
        return str(data)[:300]
    msg, params = str(data.get("message") or data), data.get("parameters") or {}
    if isinstance(params, dict):  # {"resources": "..."} fills %resources
        return re.sub(r"%(\w+)", lambda m: str(params.get(m[1], m[0])), msg)[:300]
    values = iter(params)  # a list fills the placeholders in order, whatever their names (%1, %sku, ...)
    return re.sub(r"%\w+", lambda m: str(next(values, m[0])), msg)[:300]


def _when(v: str | None) -> datetime | None:
    try:
        return datetime.strptime(v, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC) if v else None  # Magento stores UTC
    except ValueError:
        return None


def to_product(item: dict, base_url: str, default_suffix: str = ".html") -> dict:
    """A Magento GraphQL product as a normalized product (same shape the crawler stores)."""
    mp = (item.get("price_range") or {}).get("minimum_price") or {}
    final, regular = (mp.get("final_price") or {}), (mp.get("regular_price") or {})
    price, was = to_decimal(final.get("value")), to_decimal(regular.get("value"))
    cats = sorted((c for c in item.get("categories") or [] if c.get("name")), key=lambda c: -(c.get("level") or 0))
    name = item.get("name") or item.get("sku")
    suffix = item.get("url_suffix") if item.get("url_suffix") is not None else default_suffix
    return {
        "url": f"{base_url.rstrip('/')}/{item.get('url_key')}{suffix}", "title": name, "sku": item.get("sku"), "gtin": None,
        "brand": None, "category": cats[0]["name"] if cats else None, "image": (item.get("small_image") or {}).get("url"),
        "price": price, "compare_at_price": was if was and price is not None and was > price else None,
        "currency": final.get("currency") or regular.get("currency"),
        "in_stock": {"IN_STOCK": True, "OUT_OF_STOCK": False}.get(item.get("stock_status")),
        "source_date": _when(item.get("created_at")), "extracted_by": "magento",
        "meta_title": item.get("meta_title"), "meta_description": item.get("meta_description"),
        "attributes": attributes.parse(" ".join(filter(None, [name, item.get("meta_description")]))),
    }
