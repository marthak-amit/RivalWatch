"""AI fallback for product pages with no structured data and no clear price in the HTML (agent spec §1.6,
extract_product). The model only copies what the page prints: its title and price are checked against the page
text, and AI-read products never carry a sale price."""
import re
from collections.abc import Callable

from scrapling.parser import Selector

from ...crawler import attributes
from ...crawler.extract import to_decimal
from .prompts import EXTRACT, ExtractedProduct

MAX_TEXT = 6000


def _printed(price: float, text: str) -> bool:
    """Whether the page prints this price (commas removed, so 2,223.16 and Indian 7,97,204 both match)."""
    flat = text.replace(",", "")
    forms = {f"{price:.2f}"} | ({f"{price:.0f}"} if float(price).is_integer() else set())
    return any(f in flat for f in forms)


def make_extractor(llm, industry: str) -> Callable[[str, str], dict | None]:
    structured = llm.with_structured_output(ExtractedProduct)

    def extract(html: str, url: str) -> dict | None:
        text = re.sub(r"\s+", " ", str(Selector(html).get_all_text(ignore_tags=("script", "style", "noscript")))).strip()
        out = structured.invoke(EXTRACT.format(industry=industry, url=url, text=text[:MAX_TEXT]))
        p = ExtractedProduct.model_validate(out) if out is not None else None
        if not p or not p.is_product or not p.title or p.title.lower() not in text.lower():
            return None  # not a product page, or a name the page doesn't actually show
        price = p.price if p.price is not None and p.price > 0 and _printed(p.price, text) else None
        currency = p.currency.upper() if p.currency and re.fullmatch(r"[A-Za-z]{3}", p.currency) else None
        return {"url": url, "title": p.title[:300], "sku": p.sku, "gtin": None, "brand": p.brand, "category": p.category,
                "image": None, "price": to_decimal(price), "compare_at_price": None, "currency": currency,
                "in_stock": p.in_stock, "source_date": None, "extracted_by": "ai", "attributes": attributes.parse(p.title)}
    return extract
