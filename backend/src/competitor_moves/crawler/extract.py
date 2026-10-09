"""Turn fetched pages and feeds into normalized products, and pull page metadata (title, menu, promotions).

A normalized product is a dict with: url, title, sku, gtin, brand, category, image, price, compare_at_price,
currency, in_stock, source_date, extracted_by, attributes. Prices are Decimal or None, never guessed.
"""
import json
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from html import unescape
from urllib.parse import urljoin, urlsplit

from scrapling.parser import Selector

from . import attributes
from .sitemap import parse_lastmod

PROMO = re.compile(
    r"(\d+\s*%\s*off|\bsave\s+(?:up\s+to\s+)?[$€£₹]?\d|\bsale\b|\bdiscount|\bcoupon|\bpromo\b|\buse code\b|"
    r"free (?:shipping|delivery|returns|gift|engraving)|\bclearance\b|\bbogo\b|buy one,? get|limited[- ]time)", re.IGNORECASE)
_IN_STOCK = ("instock", "limitedavailability", "preorder", "presale", "backorder", "onlineonly", "instoreonly")
_OUT_OF_STOCK = ("outofstock", "soldout", "discontinued")
_MAX_TEXT = 300


def to_decimal(value) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    s = re.sub(r"[^\d.,-]", "", str(value))
    if "," in s and "." in s:
        s = s.replace(",", "")  # 1,299.00
    elif s.count(",") == 1 and len(s.split(",")[1]) == 2:
        s = s.replace(",", ".")  # 1299,00
    else:
        s = s.replace(",", "")
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    return d if d.is_finite() and d >= 0 else None


_TEMPLATE = re.compile(r"\{\{\s*[\w.]+\s*\}\}|\$\{[\w.]+\}|\[\[[\w.]+\]\]")


def templated(v) -> bool:
    """A client-side template placeholder ({{name}}, ${name}): the real value is only filled in by JavaScript."""
    return isinstance(v, str) and bool(_TEMPLATE.search(v))


def _text(v) -> str | None:
    if isinstance(v, dict):
        v = v.get("name")
    if isinstance(v, list):
        v = v[0] if v else None
    if v is None:
        return None
    s = re.sub(r"\s+", " ", unescape(str(v))).strip()
    return None if templated(s) else s[:_MAX_TEXT] or None


def _image(v) -> str | None:
    if isinstance(v, list):
        v = v[0] if v else None
    if isinstance(v, dict):
        v = v.get("url") or v.get("contentUrl") or v.get("src")
    return str(v) if v else None


def _types(node: dict) -> set[str]:
    t = node.get("@type")
    items = t if isinstance(t, list) else [t]
    return {str(x).rsplit("/", 1)[-1].lower() for x in items if x}


def _walk(data, depth: int = 0):
    if depth > 8:
        return
    if isinstance(data, list):
        for x in data:
            yield from _walk(x, depth + 1)
    elif isinstance(data, dict):
        yield data
        for v in data.values():
            if isinstance(v, (dict, list)):
                yield from _walk(v, depth + 1)


def _availability(v) -> bool | None:
    s = str(v or "").rsplit("/", 1)[-1].lower().replace(" ", "")
    if any(x in s for x in _OUT_OF_STOCK):
        return False
    if any(x in s for x in _IN_STOCK):
        return True
    return None


def _offer_fields(node: dict) -> tuple[Decimal | None, Decimal | None, str | None, bool | None]:
    """(price, compare_at_price, currency, in_stock) from offers / AggregateOffer / priceSpecification."""
    offers = node.get("offers")
    candidates = [o for o in (offers if isinstance(offers, list) else [offers]) if isinstance(o, dict)]
    best = None
    for o in candidates:
        price = to_decimal(o.get("lowPrice") if "aggregateoffer" in _types(o) else o.get("price"))
        compare = None
        specs = o.get("priceSpecification")
        for spec in (specs if isinstance(specs, list) else [specs]):
            if not isinstance(spec, dict):
                continue
            kind = str(spec.get("priceType", "")).rsplit("/", 1)[-1].lower()
            if kind in ("strikethroughprice", "listprice", "msrp"):
                compare = to_decimal(spec.get("price"))
            elif price is None:
                price = to_decimal(spec.get("price"))
        if price is None:
            continue
        currency = o.get("priceCurrency") or (specs.get("priceCurrency") if isinstance(specs, dict) else None)
        row = (price, compare if compare and compare > price else None, currency, _availability(o.get("availability")))
        if best is None or price < best[0]:
            best = row
    return best or (None, None, None, None)


def _product(node: dict, url: str, extracted_by: str) -> dict:
    if "productgroup" in _types(node) and isinstance(node.get("hasVariant"), list):
        variants = [v for v in node["hasVariant"] if isinstance(v, dict)]
        priced = [(_offer_fields(v), v) for v in variants]
        priced = [p for p in priced if p[0][0] is not None]
        offer, variant = min(priced, key=lambda p: p[0][0]) if priced else (_offer_fields(node), node)
        sku = variant.get("sku") or node.get("sku") or node.get("productGroupID")
    else:
        offer, sku = _offer_fields(node), node.get("sku")
    price, compare, currency, in_stock = offer
    title = _text(node.get("name"))
    gtin = next((node.get(k) for k in ("gtin13", "gtin12", "gtin14", "gtin8", "gtin") if node.get(k)), None)
    category = node.get("category")
    if isinstance(category, list):
        category = " > ".join(str(c) for c in category if c)
    return {
        "url": url, "title": title, "sku": _text(sku), "gtin": _text(gtin), "brand": _text(node.get("brand")),
        "category": _text(category), "image": _image(node.get("image")), "price": price,
        "compare_at_price": compare, "currency": _text(currency), "in_stock": in_stock,
        "source_date": parse_lastmod(node.get("datePublished") or node.get("releaseDate")),
        "extracted_by": extracted_by,
        "attributes": attributes.parse(" ".join(filter(None, [title, _text(node.get("description"))]))),
    }


def jsonld_nodes(html: str) -> list:
    out = []
    for raw in Selector(html).css('script[type="application/ld+json"]::text').getall():
        try:
            out.append(json.loads(raw, strict=False))
        except ValueError:
            continue
    return out


def products_from_jsonld(html: str, page_url: str) -> list[dict]:
    nodes = [n for n in _walk(jsonld_nodes(html)) if _types(n) & {"product", "productgroup"} and not templated(n.get("name"))]
    # variants of a ProductGroup are also Product nodes; keep only top-level ones
    variant_ids = {id(v) for n in nodes if "productgroup" in _types(n) for v in (n.get("hasVariant") or [])}
    nodes = [n for n in nodes if id(n) not in variant_ids]
    if len(nodes) == 1:  # a product page: the page URL is the stable key
        return [_product(nodes[0], page_url, "jsonld")]
    out = []
    for n in nodes:  # a listing page: each product needs its own URL
        u = n.get("url") or n.get("@id")
        if isinstance(u, str) and u.startswith(("http", "/")):
            out.append(_product(n, urljoin(page_url, u.split("#")[0]), "jsonld"))
    return out


def product_from_og(html: str, page_url: str) -> dict | None:
    sel = Selector(html)

    def meta(*names):
        for n in names:
            v = sel.css(f'meta[property="{n}"]::attr(content)').get() or sel.css(f'meta[name="{n}"]::attr(content)').get()
            if v:
                return v
        return None

    price = to_decimal(meta("product:price:amount", "og:price:amount"))
    if price is None:
        return None
    title = _text(meta("og:title"))
    return {
        "url": page_url, "title": title, "sku": _text(meta("product:retailer_item_id")), "gtin": None,
        "brand": _text(meta("product:brand")), "category": _text(meta("product:category")),
        "image": meta("og:image"), "price": price,
        "compare_at_price": None, "currency": _text(meta("product:price:currency", "og:price:currency")),
        "in_stock": _availability(meta("product:availability", "og:availability")), "source_date": None,
        "extracted_by": "og", "attributes": attributes.parse(title or ""),
    }


_TAG_PREFIXES = ("og:", "twitter:", "product:", "article:")
_TAG_NAMES = ("description", "keywords", "robots", "title")


def page_tags(html: str) -> dict:
    """The page's <title>, meta description and every og:/twitter:/product: tag, values exactly as on the page.
    A tag that appears more than once (e.g. og:image) becomes a list."""
    sel = Selector(html)
    tags: dict = {}
    for m in sel.css("meta"):
        key, value = (m.attrib.get("property") or m.attrib.get("name") or "").strip(), m.attrib.get("content")
        if not key or value is None or not (key.lower().startswith(_TAG_PREFIXES) or key.lower() in _TAG_NAMES):
            continue
        if key in tags:
            tags[key] = (tags[key] if isinstance(tags[key], list) else [tags[key]]) + [value]
        else:
            tags[key] = value
    title = sel.css("title::text").get()
    desc = tags.get("description")
    return {"meta_title": title.strip() if title else None,
            "meta_description": desc[0] if isinstance(desc, list) else desc, "meta": tags}


_CURRENCY = {"₹": "INR", "rs": "INR", "rs.": "INR", "inr": "INR", "$": "USD", "usd": "USD", "£": "GBP", "gbp": "GBP",
             "€": "EUR", "eur": "EUR", "aed": "AED", "a$": "AUD", "c$": "CAD"}
_AMOUNT = re.compile(r"(₹|Rs\.?|INR|A\$|C\$|\$|USD|£|GBP|€|EUR|AED)\s?(\d[\d,]*(?:\.\d{1,2})?)", re.IGNORECASE)
_SKU = re.compile(r"\b(?:SKU|Item\s*(?:No|Code)|Product\s*Code|Style\s*No)\.?\s*[:#]?\s*([A-Za-z0-9][\w-]{2,40})", re.IGNORECASE)


def _visible_text(sel: Selector) -> str:
    return re.sub(r"\s+", " ", str(sel.get_all_text(ignore_tags=("script", "style", "noscript")))).strip()


def product_from_html(html: str, page_url: str) -> dict | None:
    """Last resort for pages with no structured data: the single <h1> as the name and the price printed right after
    it. Refuses anything ambiguous (several headings, a listing of prices, templates). Never guesses a sale price."""
    sel = Selector(html, url=page_url)
    heads = [h for h in (re.sub(r"\s+", " ", str(x.get_all_text())).strip() for x in sel.css("h1")) if h]
    if len(heads) != 1 or templated(heads[0]) or len(heads[0]) > 200:
        return None
    title, text = heads[0], _visible_text(sel)
    if len(_AMOUNT.findall(text)) > 6:  # a catalogue/listing page, not one product
        return None
    for pos in reversed([m.end() for m in re.finditer(re.escape(title), text)]):
        window = text[pos: pos + 160]
        amounts = list(_AMOUNT.finditer(window))
        if not amounts:
            continue
        if len(amounts) > 1 and re.search(r"[A-Za-z]{3,}", window[amounts[0].end(): amounts[1].start()]):
            return None  # another product's name sits between two prices: a listing
        cur, amount = amounts[0].group(1), amounts[0].group(2)
        sku = _SKU.search(text[max(0, pos - 200): pos + 300])
        return {"url": page_url, "title": title[:_MAX_TEXT], "sku": sku.group(1) if sku else None, "gtin": None, "brand": None,
                "category": None, "image": None, "price": to_decimal(amount), "compare_at_price": None,
                "currency": _CURRENCY.get(cur.lower().rstrip(".") if cur.lower().startswith("rs") else cur.lower(), None),
                "in_stock": None, "source_date": None, "extracted_by": "html", "attributes": attributes.parse(title)}
    return None


def needs_browser(html: str) -> bool:
    """The product data is filled in by JavaScript: template placeholders, or almost no text without a browser."""
    if _TEMPLATE.search(html[:2_000_000]):
        return True
    return "<script" in html.lower() and len(_visible_text(Selector(html))) < 200


def extract_products(html: str, page_url: str) -> list[dict]:
    """JSON-LD, then OpenGraph product meta, then the page's own heading and price. Empty if nothing is certain.
    A product read from its own page also carries that page's meta tags."""
    found = products_from_jsonld(html, page_url)
    if not found:
        og = product_from_og(html, page_url)
        found = [og] if og else []
    if not found and not _TEMPLATE.search(html):
        p = product_from_html(html, page_url)
        found = [p] if p else []
    if len(found) == 1:
        found[0].update(page_tags(html), meta_fetched_at=datetime.now(UTC))
        if not found[0].get("category"):
            found[0]["category"] = breadcrumb_category(html, found[0].get("title"))
    return found


def _crumb_name(step: dict) -> str | None:
    item = step.get("item")
    return _text(step.get("name") or (item.get("name") if isinstance(item, dict) else None))


def breadcrumb_category(html: str, title: str | None) -> str | None:
    """The product's category from the page's breadcrumb (JSON-LD BreadcrumbList, else the HTML breadcrumb): the last
    step before the product itself, skipping Home."""
    names: list[str] = []
    for node in _walk(jsonld_nodes(html)):
        if "breadcrumblist" in _types(node) and isinstance(node.get("itemListElement"), list):
            items = sorted((i for i in node["itemListElement"] if isinstance(i, dict)), key=lambda i: i.get("position") or 0)
            names = [_crumb_name(i) for i in items]
            break
    if not names:
        sel = Selector(html)
        crumbs = sel.css('nav[aria-label*="readcrumb"] li, ol.breadcrumb li, ul.breadcrumb li, .breadcrumbs li, .breadcrumb a')
        names = [re.sub(r"\s+", " ", str(c.get_all_text())).strip() for c in crumbs]
    skip = {"home", "", (title or "").strip().lower()}
    names = [n for n in names if n and n.strip().lower() not in skip and not templated(n) and len(n) <= 60]
    return names[-1] if names else None


def shopify_product(p: dict, base: str) -> dict:
    variants = [v for v in (p.get("variants") or []) if isinstance(v, dict)]
    priced = [(to_decimal(v.get("price")), v) for v in variants]
    priced = [x for x in priced if x[0] is not None]
    price, v0 = min(priced, key=lambda x: x[0]) if priced else (None, {})
    compare = to_decimal(v0.get("compare_at_price"))
    title = _text(p.get("title"))
    has_flag = any("available" in v for v in variants)
    return {
        "url": f"{base.rstrip('/')}/products/{p.get('handle')}", "title": title, "sku": _text(v0.get("sku")),
        "gtin": _text(v0.get("barcode")), "brand": _text(p.get("vendor")), "category": _text(p.get("product_type")),
        "image": _image(p.get("images")), "price": price,
        "compare_at_price": compare if compare and price is not None and compare > price else None,
        "currency": None, "in_stock": any(v.get("available") for v in variants) if has_flag else None,
        "source_date": parse_lastmod(p.get("published_at") or p.get("created_at")), "extracted_by": "feed",
        "attributes": attributes.parse(" ".join(filter(None, [title, _text(p.get("product_type"))]))),
    }


def woo_product(p: dict) -> dict:
    prices = p.get("prices") or {}
    scale = Decimal(10) ** int(prices.get("currency_minor_unit", 2) or 0)
    raw = lambda k: (to_decimal(prices.get(k)) / scale) if to_decimal(prices.get(k)) is not None else None
    price, regular = raw("price"), raw("regular_price")
    title = _text(p.get("name"))
    cats = p.get("categories") or []
    return {
        "url": p.get("permalink"), "title": title, "sku": _text(p.get("sku")), "gtin": None, "brand": None,
        "category": _text(cats[0]) if cats else None, "image": _image(p.get("images")), "price": price,
        "compare_at_price": regular if regular and price is not None and regular > price else None,
        "currency": _text(prices.get("currency_code")), "in_stock": p.get("is_in_stock"),
        "source_date": None, "extracted_by": "feed", "attributes": attributes.parse(title or ""),
    }


def same_site(url: str, base: str) -> bool:
    a, b = urlsplit(url).hostname or "", urlsplit(base).hostname or ""
    return a.removeprefix("www.") == b.removeprefix("www.")


def page_meta(html: str, page_url: str) -> dict:
    """Title, description, first h1, promotion lines and the site menu (same-site links in nav/header)."""
    sel = Selector(html, url=page_url)
    lines = [re.sub(r"\s+", " ", x).strip() for x in str(sel.get_all_text(ignore_tags=("script", "style"))).split("\n")]
    promos = list(dict.fromkeys(x[:160] for x in lines if 3 < len(x) <= 200 and PROMO.search(x)))[:10]
    nav, seen = [], set()
    for a in sel.css("nav a, header a"):
        href = a.attrib.get("href") or ""
        name = re.sub(r"\s+", " ", str(a.get_all_text())).strip()
        url = urljoin(page_url, href).split("#")[0]
        if not href or href.startswith(("javascript:", "mailto:", "tel:")) or not (1 <= len(name) <= 60):
            continue
        if url in seen or not same_site(url, page_url) or url.rstrip("/") == page_url.split("#")[0].rstrip("/"):
            continue  # a menu link back to this (home) page is the brand, not a category
        seen.add(url)
        nav.append({"name": name, "url": url})
    return {
        "title": _text(sel.css("title::text").get()),
        "description": _text(sel.css('meta[name="description"]::attr(content)').get()),
        "h1": _text(sel.css("h1::text").get()),
        "promotions": promos,
        "nav": nav[:80],
    }


def links(html: str, page_url: str) -> list[str]:
    """All same-site links on the page, absolute and without fragments."""
    out = []
    for href in Selector(html, url=page_url).css("a::attr(href)").getall():
        if href.startswith(("javascript:", "mailto:", "tel:")):
            continue
        u = urljoin(page_url, href).split("#")[0]
        if same_site(u, page_url):
            out.append(u)
    return list(dict.fromkeys(out))


SOCIAL = {"x.com": "X", "twitter.com": "X", "linkedin.com": "LinkedIn", "facebook.com": "Facebook",
          "instagram.com": "Instagram", "youtube.com": "YouTube", "github.com": "GitHub", "tiktok.com": "TikTok",
          "pinterest.com": "Pinterest"}
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def profile(html: str, page_url: str, nav: list[dict] | None = None) -> dict:
    """Who the competitor is, from its homepage (the UI's company card): headline, description, positioning
    headings, social accounts, contact emails and key pages."""
    sel = Selector(html, url=page_url)
    heads = [re.sub(r"\s+", " ", str(h.get_all_text())).strip() for h in sel.css("h1, h2, h3")]
    heads = [h for h in dict.fromkeys(heads) if 2 < len(h) <= 160]
    desc = (sel.css('meta[name="description"]::attr(content)').get() or sel.css('meta[property="og:description"]::attr(content)').get()
            or "")
    socials, seen = [], set()
    for href in sel.css("a::attr(href)").getall():
        host = (urlsplit(href).hostname or "").removeprefix("www.")
        net = SOCIAL.get(host) or next((v for k, v in SOCIAL.items() if host.endswith("." + k)), None)
        if net and net not in seen and len(urlsplit(href).path) > 1:
            seen.add(net)
            socials.append({"network": net, "url": href})
    mails = [m.removeprefix("mailto:").split("?")[0].lower() for m in sel.css('a[href^="mailto:"]::attr(href)').getall()]
    mails += [m.lower() for m in _EMAIL.findall(str(sel.get_all_text()))]
    emails = [m for m in dict.fromkeys(mails) if _EMAIL.fullmatch(m) and not re.search(r"\.(png|jpe?g|gif|svg|webp)$", m)][:2]
    key_pages = [{"path": urlsplit(n["url"]).path or "/", "title": _clip(n["name"], 40)} for n in (nav or [])[:8]]
    return {"headline": _clip(heads[0] if heads else (_text(sel.css("title::text").get()) or ""), 120),
            "description": _clip(unescape(desc).strip(), 280), "positioning": [_clip(h, 70) for h in heads[1:5]],
            "socials": socials[:6], "emails": emails, "keyPages": key_pages}

