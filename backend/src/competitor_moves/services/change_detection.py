"""Turn what a crawl read into change events (rules: spec §5.1) and log them in the site's `history` table.

Pure functions first, then the DB writes that use them. Events are dicts {type, category, before, after,
occurred_at}; product_id, run_id and user_id are added when they are stored.
"""
import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlsplit

from psycopg import sql
from psycopg.types.json import Jsonb

from ..db.site_schema import table

TRACKED = ("title", "price", "compare_at_price", "in_stock")
FIELDS = ("title", "sku", "sku_norm", "gtin", "brand", "category", "image", "price", "compare_at_price", "currency",
          "in_stock", "source_date", "extracted_by", "attributes", "path", "source_url", "meta_title", "meta_description",
          "meta", "meta_fetched_at")
MAX_PAGE_EVENTS = 50  # a site restructure must not flood the feed
DETAIL_FIELDS = ("title", "sku", "gtin", "brand", "category", "image", "currency", "meta_title",
                 "meta_description")  # logged as product_updated


def _dumps(v) -> str:
    return json.dumps(v, default=str)  # dates and Decimals inside before/after


def money(v) -> float | None:
    return None if v is None else round(float(v), 2)


def norm_sku(sku: str | None) -> str | None:
    s = re.sub(r"[^a-z0-9]", "", (sku or "").lower())
    return s or None


def product_hash(p: dict) -> str:
    def norm(v):
        return f"{Decimal(v).normalize():f}" if isinstance(v, (Decimal, int, float)) and not isinstance(v, bool) else str(v)
    return hashlib.sha1("|".join(norm(p.get(k)) for k in TRACKED).encode()).hexdigest()


def merge(old: dict | None, new: dict) -> dict:
    """The product as it should be stored: new values win, but a value the new read lacks keeps the old one."""
    if old is None:
        return dict(new)
    return {**new, **{k: old.get(k) for k in FIELDS if new.get(k) is None and old.get(k) is not None}}


def _on_sale(p: dict) -> bool:
    return p.get("price") is not None and p.get("compare_at_price") is not None and p["compare_at_price"] > p["price"]


def _snap(p: dict) -> dict:
    out = {"title": p.get("title"), "price": money(p.get("price")), "currency": p.get("currency")}
    if p.get("compare_at_price") is not None:
        out["compare_at_price"] = money(p["compare_at_price"])
    return out


def product_events(old: dict | None, new: dict, *, now: datetime, window_start: datetime,
                   prev_full_coverage: bool) -> list[dict]:
    cat, out = new.get("category") or (old or {}).get("category"), []

    def add(kind, before, after, at=now):
        out.append({"type": kind, "category": cat, "before": before, "after": after, "occurred_at": at})

    if old is None:
        src = new.get("source_date")
        if src is not None and src >= window_start:
            add("new_product", None, _snap(new), src)
        elif src is None and prev_full_coverage:  # we saw the whole catalog last time, so this one is new
            add("new_product", None, _snap(new))
    if _on_sale(new) and not (old and _on_sale(old)):
        pct = round(float((new["compare_at_price"] - new["price"]) / new["compare_at_price"] * 100), 1)
        add("on_sale", None, {**_snap(new), "pct_off": pct})
    if old is not None:
        a, b = old.get("price"), new.get("price")
        if a is not None and b is not None and a != b:
            pct = round(float((b - a) / a * 100), 1) if a else None
            add("price_drop" if b < a else "price_rise", _snap(old), {**_snap(new), "pct": pct})
        if old.get("in_stock") is True and new.get("in_stock") is False:
            add("out_of_stock", _snap(old), _snap(new))
        elif old.get("in_stock") is False and new.get("in_stock") is True:
            add("back_in_stock", _snap(old), _snap(new))
        changed = [k for k in DETAIL_FIELDS if new.get(k) is not None and old.get(k) is not None
                   and _detail(k, old[k]) != _detail(k, new[k])]
        if changed:
            add("product_updated", {k: old[k] for k in changed}, {k: new[k] for k in changed})
    return out


def _detail(field: str, value) -> str:
    v = str(value).strip()
    return v.split("?", 1)[0] if field == "image" else v  # CDN images change their ?v= query on every re-upload


def home_events(old: dict | None, new: dict, *, now: datetime) -> list[dict]:
    """Homepage and menu changes. The first look at a site is a baseline, not a change."""
    if old is None:
        return []
    out = []
    old_p, new_p = old.get("promotions") or [], new.get("promotions") or []
    out += [{"type": "promo_started", "category": None, "before": None, "after": {"text": t}, "occurred_at": now}
            for t in new_p if t not in old_p]
    out += [{"type": "promo_ended", "category": None, "before": {"text": t}, "after": None, "occurred_at": now}
            for t in old_p if t not in new_p]
    old_nav = {n["url"] for n in old.get("nav") or []}
    out += [{"type": "new_category", "category": n["name"], "before": None, "after": {"name": n["name"], "url": n["url"]},
             "occurred_at": now} for n in new.get("nav") or [] if n["url"] not in old_nav]

    def view(h):
        return {"title": h.get("title"), "h1": h.get("h1"), "promotions": h.get("promotions") or [],
                "nav": [n["name"] for n in h.get("nav") or []]}
    if view(old) != view(new):
        out.append({"type": "page_changed", "category": None, "before": view(old), "after": view(new), "occurred_at": now})
    return out


_LANDING_FILE = re.compile(r"page|collection|categor|blog|post|landing", re.IGNORECASE)
_LANDING_PATH = re.compile(r"^/(pages|collections|blogs|landing|campaigns?|promotions?)(/|$)", re.IGNORECASE)
_PRODUCT_PATH = re.compile(r"^/(products?|p)/", re.IGNORECASE)


def is_landing_page(url: str, sitemap_file: str) -> bool:
    """Non-product pages worth tracking as new/removed pages. ponytail: name heuristics, not page analysis."""
    path = urlsplit(url).path or "/"
    if _PRODUCT_PATH.match(path) or path.endswith(".html") and not _LANDING_PATH.match(path):
        return False
    return bool(_LANDING_PATH.match(path) or _LANDING_FILE.search(urlsplit(sitemap_file).path.rsplit("/", 1)[-1]))


def page_events(known: set[str], current: set[str], *, complete: bool, first_run: bool, now: datetime) -> list[dict]:
    if first_run:
        return []

    def ref(u):
        return {"path": urlsplit(u).path or "/", "url": u}
    out = [{"type": "new_page", "category": None, "before": None, "after": ref(u), "occurred_at": now}
           for u in sorted(current - known)[:MAX_PAGE_EVENTS]]
    if complete:  # only a full read can prove a page is gone
        out += [{"type": "page_removed", "category": None, "before": ref(u), "after": None, "occurred_at": now}
                for u in sorted(known - current)[:MAX_PAGE_EVENTS]]
    return out


# ---- DB writes (one site's schema) ----------------------------------------------------------------------------

def _price(side) -> float | None:
    return side.get("price") if isinstance(side, dict) else None


def store_events(conn, site_id: int, run_id: int | None, events: list[dict], product_id: int | None = None,
                 user_id: int | None = None, product: dict | None = None, source_url: str | None = None) -> list[int]:
    """Append to the site's history log. run_id is the detecting crawl; user_id who made the change, if a person did.

    `product` (title, url, currency, source_url) fills the plain columns; for page/menu/promotion changes the URL
    comes from the event itself and `source_url` names the page it was seen on.
    """
    product = product or {}
    ids = []
    for e in events:
        side = e["after"] if isinstance(e["after"], dict) else e["before"] if isinstance(e["before"], dict) else {}
        url = product.get("url") or side.get("url")
        row = conn.execute(sql.SQL(
            "insert into {} (run_id, product_id, user_id, type, category, before, after, occurred_at, "
            "title, url, path, source_url, currency, price_before, price_after) "
            "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id").format(table(site_id, "history")),
            (run_id, product_id, user_id, e["type"], e["category"],
             Jsonb(e["before"], dumps=_dumps) if e["before"] is not None else None,
             Jsonb(e["after"], dumps=_dumps) if e["after"] is not None else None, e["occurred_at"],
             product.get("title") or side.get("title") or side.get("name") or side.get("text"),
             url, urlsplit(url).path if url else None, product.get("source_url") or source_url,
             product.get("currency") or side.get("currency"), _price(e["before"]), _price(e["after"]))).fetchone()
        ids.append(row["id"])
    return ids


def record_product(conn, site_id: int, run_id: int | None, item: dict, *, now: datetime, window_start: datetime,
                   prev_full_coverage: bool, user_id: int | None = None) -> list[dict]:
    """Upsert one product read by a crawl (or changed by a person: user_id, no run), append price history if it
    changed, store and return its events."""
    products = table(site_id, "products")
    old = conn.execute(sql.SQL("select * from {} where url=%s").format(products), (item["url"],)).fetchone()
    events = product_events(old, item, now=now, window_start=window_start, prev_full_coverage=prev_full_coverage)
    p = merge(old, {**item, "sku_norm": norm_sku(item.get("sku")), "path": urlsplit(item["url"]).path or "/"})
    h = product_hash(p)
    values = [Jsonb(p.get(k) or {}) if k == "attributes" else Jsonb(p[k]) if k == "meta" and p.get(k) is not None
              else p.get(k) for k in FIELDS]
    with conn.transaction():
        if old is None:
            cols = sql.SQL(", ").join(map(sql.Identifier, FIELDS))
            pid = conn.execute(sql.SQL(
                "insert into {} (url, {}, hash, last_checked_at) values (%s, {}, %s, %s) returning id").format(
                products, cols, sql.SQL(", ").join(sql.Placeholder() * len(FIELDS))),
                [item["url"], *values, h, now]).fetchone()["id"]
        else:
            pid = old["id"]
            sets = sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(k)) for k in FIELDS)
            conn.execute(sql.SQL(
                "update {} set {}, hash=%s, last_seen_at=%s, last_checked_at=%s, missing_count=0, removed_at=null "
                "where id=%s").format(products, sets), [*values, h, now, now, pid])
        if old is None or old.get("hash") != h:
            conn.execute(sql.SQL(
                "insert into {} (product_id, run_id, price, compare_at_price, in_stock, hash, seen_at) "
                "values (%s,%s,%s,%s,%s,%s,%s)").format(table(site_id, "price_history")),
                (pid, run_id, p.get("price"), p.get("compare_at_price"), p.get("in_stock"), h, now))
        ids = store_events(conn, site_id, run_id, events, pid, user_id=user_id, product={**p, "url": item["url"]})
    return [{**e, "id": i, "product_id": pid} for e, i in zip(events, ids, strict=True)]


def mark_missing(conn, site_id: int, run_id: int, seen_urls: set[str], *, now: datetime) -> list[dict]:
    """After a run that read the whole catalog: products not seen twice in a row become product_removed."""
    products = table(site_id, "products")
    rows = conn.execute(sql.SQL(
        "update {} set missing_count = missing_count + 1 where removed_at is null and not (url = any(%s)) "
        "returning id, url, title, category, price, currency, source_url, missing_count").format(products),
        (list(seen_urls),)).fetchall()
    out = []
    for r in rows:
        if r["missing_count"] >= 2:
            conn.execute(sql.SQL("update {} set removed_at=%s where id=%s").format(products), (now, r["id"]))
            e = {"type": "product_removed", "category": r["category"], "before": _snap(r), "after": None, "occurred_at": now}
            [eid] = store_events(conn, site_id, run_id, [e], r["id"], product=r)
            out.append({**e, "id": eid, "product_id": r["id"]})
    return out


def record_meta(conn, site_id: int, run_id: int, url: str, tags: dict, *, now: datetime) -> list[dict]:
    """Store a product page's meta tags; a changed meta title/description is logged as product_updated."""
    products = table(site_id, "products")
    old = conn.execute(sql.SQL("select id, url, title, currency, source_url, meta_title, meta_description from {} "
                               "where url=%s").format(products), (url,)).fetchone()
    if old is None:
        return []
    conn.execute(sql.SQL("update {} set meta_title=%s, meta_description=%s, meta=%s, meta_fetched_at=%s where id=%s").format(
        products), (tags["meta_title"], tags["meta_description"], Jsonb(tags["meta"]), now, old["id"]))
    changed = [k for k in ("meta_title", "meta_description")
               if old[k] is not None and tags[k] is not None and old[k].strip() != tags[k].strip()]
    if not changed:
        return []
    e = {"type": "product_updated", "category": None, "before": {k: old[k] for k in changed},
         "after": {k: tags[k] for k in changed}, "occurred_at": now}
    [eid] = store_events(conn, site_id, run_id, [e], old["id"], product=old)
    return [{**e, "id": eid, "product_id": old["id"]}]

