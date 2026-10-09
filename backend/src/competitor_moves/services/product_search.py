"""Search the products collected from a user's competitors, with the filters a jewellery shop shows on its own
category pages: category, metal, gemstone, stone type, price range, on sale, in stock, text, and a sort."""
import re
from dataclasses import dataclass

from psycopg import sql

from ..db.site_schema import table
from .dashboard import money

SORT_SQL = {
    "relevance": "last_seen_at desc, id desc",
    "newest": "source_date desc nulls last, first_seen_at desc, id desc",
    "price_asc": "price asc nulls last, id",
    "price_desc": "price desc nulls last, id",
    "discount": "(case when compare_at_price > price then (compare_at_price - price) / compare_at_price else 0 end) desc, id",
}
SORT_KEY = {
    "relevance": lambda p: (-p["_last"].timestamp(), -p["_id"]),
    "newest": lambda p: (p["_src"] is None, -(p["_src"].timestamp() if p["_src"] else 0), -p["_first"].timestamp()),
    "price_asc": lambda p: (p["price"] is None, p["price"] or 0),
    "price_desc": lambda p: (p["price"] is None, -(p["price"] or 0)),
    "discount": lambda p: -(p["discountPct"] or 0),
}


@dataclass
class Filters:
    category: str | None = None
    metal: str | None = None  # contains: "gold" matches "18k gold plated"
    gem: str | None = None
    stone: str | None = None
    min_price: float | None = None
    max_price: float | None = None
    on_sale: bool | None = None
    in_stock: bool | None = None
    q: str | None = None
    sort: str = "relevance"


def _where(f: Filters) -> tuple[sql.Composable, list]:
    conds, args = [sql.SQL("removed_at is null")], []
    if f.category:
        conds.append(sql.SQL("lower(category) = lower(%s)"))
        args.append(f.category)
    if f.metal:
        conds.append(sql.SQL("attributes->>'metal' ilike %s"))
        args.append(f"%{f.metal}%")
    for key, value in (("gem", f.gem), ("stone", f.stone)):
        if value:
            conds.append(sql.SQL("lower(attributes->>{}) = lower(%s)").format(sql.Literal(key)))
            args.append(value)
    if f.min_price is not None:
        conds.append(sql.SQL("price >= %s"))
        args.append(f.min_price)
    if f.max_price is not None:
        conds.append(sql.SQL("price <= %s"))
        args.append(f.max_price)
    if f.on_sale is not None:
        conds.append(sql.SQL("(compare_at_price > price) = %s"))
        args.append(f.on_sale)
    if f.in_stock is not None:
        conds.append(sql.SQL("in_stock = %s"))
        args.append(f.in_stock)
    for word in re.findall(r"\w+", f.q or "")[:8]:  # every word must start a word in the title: "ring" ≠ "earrings"
        conds.append(sql.SQL("title ~* %s"))
        args.append(r"\m" + word)
    return sql.SQL(" and ").join(conds), args


def search(conn, sites: list[dict], f: Filters, *, limit: int = 50, offset: int = 0) -> dict:
    where, args = _where(f)
    order = sql.SQL(SORT_SQL[f.sort])
    rows, total = [], 0
    for s in sites:
        t = table(s["id"], "products")
        total += conn.execute(sql.SQL("select count(*) as n from {} where {}").format(t, where), args).fetchone()["n"]
        for r in conn.execute(sql.SQL(
                "select id, sku, title, url, path, price, compare_at_price, currency, in_stock, category, attributes, image, "
                "meta_title, source_date, first_seen_at, last_seen_at from {} where {} order by {} limit %s").format(t, where, order),
                [*args, offset + limit]).fetchall():
            was = r["compare_at_price"] if r["compare_at_price"] and r["price"] is not None and r["compare_at_price"] > r["price"] else None
            rows.append({
                "competitorId": str(s["id"]), "competitor": s["name"] or s["domain"], "sku": r["sku"], "title": r["title"], "url": r["url"],
                "path": r["path"], "price": float(r["price"]) if r["price"] is not None else None,
                "priceLabel": money(r["price"], r["currency"]), "wasPrice": money(was, r["currency"]),
                "discountPct": round(float((was - r["price"]) / was * 100), 1) if was else None, "currency": r["currency"],
                "inStock": r["in_stock"], "category": r["category"], "attributes": r["attributes"], "image": r["image"],
                "metaTitle": r["meta_title"], "firstSeen": r["first_seen_at"].isoformat(),
                "_id": r["id"], "_src": r["source_date"], "_first": r["first_seen_at"], "_last": r["last_seen_at"]})
    rows.sort(key=SORT_KEY[f.sort])
    page = [{k: v for k, v in p.items() if not k.startswith("_")} for p in rows[offset: offset + limit]]
    return {"products": page, "total": total, "limit": limit, "offset": offset, "facets": facets(conn, sites)}


def facets(conn, sites: list[dict]) -> dict:
    """The values each filter can take across these competitors, for the filter dropdowns."""
    out = {"categories": set(), "metals": set(), "gems": set(), "stones": set(), "currencies": set()}
    for s in sites:
        r = conn.execute(sql.SQL(
            "select array_agg(distinct category) filter (where category is not null) as categories, "
            "array_agg(distinct attributes->>'metal') filter (where attributes->>'metal' is not null) as metals, "
            "array_agg(distinct attributes->>'gem') filter (where attributes->>'gem' is not null) as gems, "
            "array_agg(distinct attributes->>'stone') filter (where attributes->>'stone' is not null) as stones, "
            "array_agg(distinct currency) filter (where currency is not null) as currencies "
            "from {} where removed_at is null").format(table(s["id"], "products"))).fetchone()
        for k in out:
            out[k] |= set(r[k] or [])
    return {k: sorted(v) for k, v in out.items()}
