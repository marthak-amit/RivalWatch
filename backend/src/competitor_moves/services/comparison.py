"""Our store against competitors: products grouped by jewellery type (and optionally metal, stone, carat band),
median/min/max price per group, the gap against ours, exact SKU/barcode matches, and what only competitors sell.

All in code; the AI analyst only words what comes out of here. Prices in different currencies are compared only when
the workspace has a rate for that currency (compare_settings.fx_rates); otherwise they are shown but not compared.
"""
import re
import statistics
from collections import defaultdict

from psycopg import sql

from ..db.site_schema import table

DEFAULTS = {"min_price": 10.0, "min_group_size": 3, "fx_rates": {}}
GROUP_BY = {"type": ("type",), "type,metal": ("type", "metal"), "type,stone": ("type", "stone"),
            "type,metal,stone": ("type", "metal", "stone"), "type,carat": ("type", "carat")}

# most specific first; matched on the shop's category, then on the title when the category is generic
_TYPES = [("set", r"\bsets?\b|\bbundles?\b"), ("engagement ring", r"engagement"),
          ("wedding band", r"wedding band|wedding ring|eternity band|\bbands?\b"),
          ("earrings", r"earring|\bstuds?\b|\bhoops?\b|huggies|\bdrops?\b|jhumka"),
          ("pendant", r"pendant|\bcharms?\b|locket|mangalsutra"), ("necklace", r"necklace|\bchains?\b|choker|lariat"),
          ("bangle", r"bangle|\bkada\b"), ("bracelet", r"bracelet|wristband|\bcuffs?\b"), ("anklet", r"anklet"),
          ("watch", r"\bwatch(es)?\b"), ("brooch", r"brooch"),
          ("loose diamond", r"loose (lab[- ]grown |natural )?diamond|loose stones?|\bloose\b"), ("ring", r"\brings?\b")]


def product_type(title: str | None, category: str | None) -> str | None:
    for text in (category, title):
        for name, pat in _TYPES:
            if text and re.search(pat, text, re.IGNORECASE):
                return name
    if title and re.search(r"\b(round|oval|pear|princess|cushion|emerald|marquise|radiant|asscher)\b.*\bdiamond\b|"
                           r"\bcertified diamond\b", title, re.IGNORECASE):
        return "loose diamond"
    return None


def metal_family(metal: str | None) -> str | None:
    if not metal:
        return None
    m = metal.lower()
    if "plated" in m or "vermeil" in m:
        return "gold plated" if "gold" in m else "plated"
    for fam in ("gold", "platinum", "silver", "palladium", "titanium", "tungsten"):
        if fam in m:
            return fam
    return None


def stone_band(attrs: dict) -> str | None:
    stone, gem = attrs.get("stone"), attrs.get("gem")
    if stone == "lab_grown":
        return "lab-grown diamond"
    if stone == "natural":
        return "natural diamond"
    if stone == "moissanite":
        return "moissanite"
    if gem == "cubic zirconia":
        return "cubic zirconia"
    if gem == "diamond":
        return "diamond"
    return "gemstone" if gem else None


def carat_band(carat: float | None) -> str | None:
    if carat is None:
        return None
    return "<0.5ct" if carat < 0.5 else "0.5-1ct" if carat < 1 else "1-2ct" if carat < 2 else "2ct+"


def median(values: list[float]) -> float:
    return float(statistics.median(values))


def gap_pct(theirs: float, ours: float) -> float:
    return round((theirs - ours) / ours * 100, 1)


def settings_of(project: dict) -> dict:
    own = project.get("compare_settings") or {}
    return {**DEFAULTS, **{k: v for k, v in own.items() if k in DEFAULTS}}


def _products(conn, site_id: int) -> list[dict]:
    return conn.execute(sql.SQL(
        "select id, title, url, sku_norm, gtin, category, price, currency, attributes from {} "
        "where removed_at is null and price is not null").format(table(site_id, "products"))).fetchall()


def _key(p: dict, dims: tuple[str, ...]) -> tuple | None:
    a = p["attributes"] or {}
    parts = {"type": product_type(p["title"], p["category"]), "metal": metal_family(a.get("metal")),
             "stone": stone_band(a), "carat": carat_band(a.get("carat"))}
    if parts["type"] is None:
        return None
    return tuple(parts[d] or "unspecified" for d in dims)


def _site_currency(rows: list[dict]) -> str | None:
    found = [r["currency"] for r in rows if r["currency"]]
    return max(set(found), key=found.count) if found else None


def _stats(prices: list[float]) -> dict:
    return {"count": len(prices), "median": round(median(prices), 2), "min": round(min(prices), 2), "max": round(max(prices), 2)}


def compare(conn, project: dict, ours: dict | None, competitors: list[dict], *, group_by: str = "type",
            category: str | None = None) -> dict:
    s = settings_of(project)
    dims = GROUP_BY.get(group_by, GROUP_BY["type"])
    if ours is None:
        return {"ours": None, "settings": s, "groupBy": group_by, "rows": [], "onlyCompetitors": [], "matches": [],
                "notes": ["Connect your store (PUT /api/store) to compare prices."]}
    our_rows = [p for p in _products(conn, ours["id"]) if float(p["price"]) >= s["min_price"]]
    our_cur = _site_currency(our_rows)
    notes, groups = [], defaultdict(lambda: {"ours": [], "theirs": defaultdict(list)})
    for p in our_rows:
        if (k := _key(p, dims)) is not None:
            groups[k]["ours"].append(float(p["price"]))
    their_rows, rates = {}, {}
    for c in competitors:
        rows = _products(conn, c["id"])
        cur = _site_currency(rows) or our_cur
        rate = 1.0 if cur == our_cur else s["fx_rates"].get(cur)
        rates[c["id"]] = (cur, rate)
        if rate is None:
            notes.append(f"{c['name'] or c['domain']} prices are in {cur}; add an fx_rates entry for {cur} to compare them "
                         f"with your {our_cur} prices.")
        their_rows[c["id"]] = rows
        for p in rows:
            price = float(p["price"]) * (rate or 1.0)
            if rate is not None and price < s["min_price"]:
                continue
            if (k := _key(p, dims)) is not None:
                groups[k]["theirs"][c["id"]].append((float(p["price"]), price))
    names = {c["id"]: c["name"] or c["domain"] for c in competitors}
    rows_out, only_them = [], []
    for k, g in groups.items():
        label = " · ".join(x for x in k if x != "unspecified") or k[0]
        if category and k[0] != category:
            continue
        theirs = []
        for cid, pairs in g["theirs"].items():
            cur, rate = rates[cid]
            if len(pairs) < s["min_group_size"]:
                continue
            native, converted = [p[0] for p in pairs], [p[1] for p in pairs]
            entry = {"competitorId": str(cid), "name": names[cid], "currency": cur, **_stats(native), "comparable": rate is not None}
            if rate is not None and len(g["ours"]) >= s["min_group_size"]:
                entry["medianInOurCurrency"] = round(median(converted), 2)
                entry["gapPct"] = gap_pct(entry["medianInOurCurrency"], median(g["ours"]))
            theirs.append(entry)
        if len(g["ours"]) >= s["min_group_size"] and theirs:
            comparable = [t for t in theirs if "gapPct" in t]
            cheapest = min(comparable, key=lambda t: t["gapPct"]) if comparable else None
            rows_out.append({"key": dict(zip(dims, k, strict=True)), "label": label,
                             "ours": {"currency": our_cur, **_stats(g["ours"])}, "competitors": theirs,
                             "cheapest": "ours" if not cheapest or cheapest["gapPct"] >= 0 else cheapest["competitorId"]})
        elif not g["ours"] and theirs:
            only_them.append({"key": dict(zip(dims, k, strict=True)), "label": label, "competitors": theirs})
    rows_out.sort(key=lambda r: -max((abs(t.get("gapPct", 0)) for t in r["competitors"]), default=0))
    only_them.sort(key=lambda r: -sum(t["count"] for t in r["competitors"]))
    return {"ours": {"siteId": str(ours["id"]), "name": ours["name"], "currency": our_cur, "products": len(our_rows)},
            "settings": s, "groupBy": group_by, "rows": rows_out, "onlyCompetitors": only_them,
            "matches": _matches(conn, our_rows, their_rows, names, rates, our_cur), "notes": notes}


def _matches(conn, our_rows, their_rows, names, rates, our_cur) -> list[dict]:
    """The same product on both sides: exact normalized SKU or barcode."""
    by_sku = {p["sku_norm"]: p for p in our_rows if p["sku_norm"]}
    by_gtin = {p["gtin"]: p for p in our_rows if p["gtin"]}
    out = []
    for cid, rows in their_rows.items():
        cur, rate = rates[cid]
        for p in rows:
            o = (by_gtin.get(p["gtin"]) if p["gtin"] else None) or (by_sku.get(p["sku_norm"]) if p["sku_norm"] else None)
            if not o:
                continue
            theirs = float(p["price"]) * rate if rate is not None else None
            out.append({"sku": o["sku_norm"], "title": o["title"], "ours": {"price": float(o["price"]), "currency": our_cur, "url": o["url"]},
                        "competitorId": str(cid), "competitor": names[cid],
                        "theirs": {"price": float(p["price"]), "currency": cur, "url": p["url"]},
                        "gapPct": gap_pct(theirs, float(o["price"])) if theirs is not None else None})
    return sorted(out, key=lambda m: m["gapPct"] if m["gapPct"] is not None else 0)
