"""Group a site's recent events into moves ("Launched 14 products in Rings") with sizes computed in code."""
import re
from collections import Counter

MOVE_OF = {"new_product": "launched_products", "new_category": "new_category", "on_sale": "discounting",
           "price_drop": "discounting", "promo_started": "discounting", "price_rise": "price_increase",
           "out_of_stock": "stock_out", "product_removed": "removed_products"}
MAX_EXAMPLES = 5


def group(events: list[dict]) -> list[dict]:
    """events need: id, type, product_id, category, before, after, occurred_at, title, url."""
    groups: dict[str, list[dict]] = {}
    for e in events:
        kind = MOVE_OF.get(e["type"])
        if kind:
            cat = None if kind == "discounting" and e["type"] == "promo_started" else e.get("category")
            groups.setdefault(f"{kind}|{cat or ''}", []).append(e)
    return [_move(key, evs) for key, evs in groups.items()]


def _move(key: str, evs: list[dict]) -> dict:
    kind, cat = key.split("|", 1)
    products = {e["product_id"] for e in evs if e["product_id"] is not None}
    prices = [e["after"]["price"] for e in evs if (e.get("after") or {}).get("price") is not None]
    currency = Counter(c for e in evs if (c := (e.get("after") or {}).get("currency"))).most_common(1)
    size = {"count": len(products) if products else len(evs)}
    if prices:
        size.update(min_price=round(min(prices), 2), max_price=round(max(prices), 2))
    if currency:
        size["currency"] = currency[0][0]
    if kind == "discounting":
        best: dict[int, float] = {}  # per product, the deepest cut seen
        for e in evs:
            a = e.get("after") or {}
            off = a.get("pct_off") if e["type"] == "on_sale" else (-a["pct"] if a.get("pct") is not None else None)
            if off is not None and e["product_id"] is not None:
                best[e["product_id"]] = max(best.get(e["product_id"], 0), off)
        if best:
            size["avg_pct_off"] = round(sum(best.values()) / len(best), 1)
    if kind == "price_increase":
        ups = [e["after"]["pct"] for e in evs if (e.get("after") or {}).get("pct") is not None]
        if ups:
            size["avg_pct_up"] = round(sum(ups) / len(ups), 1)
    examples = [{"event_id": e["id"], "type": e["type"], "title": e.get("title"), "url": e.get("url"),
                 "before": e.get("before"), "after": e.get("after"), "occurred_at": e["occurred_at"]}
                for e in evs if e["product_id"] is not None][:MAX_EXAMPLES]
    return {"key": key, "type": kind, "category": cat or None, "size": size, "examples": examples,
            "promotions": [e["after"]["text"] for e in evs if e["type"] == "promo_started"],
            "evidence_event_ids": [e["id"] for e in evs]}


def _n(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def template_headline(m: dict) -> str:
    n, cat = m["size"]["count"], m["category"]
    in_cat = f" in {cat}" if cat else ""
    if m["type"] == "launched_products":
        return f"Launched {_n(n, 'product')}{in_cat}"
    if m["type"] == "new_category":
        return f"Opened a new category: {cat}"
    if m["type"] == "discounting":
        if not m["examples"] and m["promotions"]:
            return f"Started a promotion: “{m['promotions'][0]}”"
        off = m["size"].get("avg_pct_off")
        return f"Cut prices on {_n(n, 'product')}{in_cat}" + (f", {off:g}% off on average" if off else "")
    if m["type"] == "price_increase":
        return f"Raised prices on {_n(n, 'product')}{in_cat}"
    if m["type"] == "stock_out":
        return f"Ran out of {_n(n, 'product')}{in_cat}"
    if m["type"] == "removed_products":
        return f"Removed {_n(n, 'product')}{in_cat}"
    return f"{m['type']}{in_cat}"


_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> list[float]:
    return [float(x.replace(",", "")) for x in _NUM.findall(text or "")]


def numbers_ok(text: str, m: dict) -> bool:
    """Every number in AI text must come from the move's facts (size, category, example titles)."""
    allowed = [float(v) for v in m["size"].values() if isinstance(v, (int, float)) and not isinstance(v, bool)]
    allowed += _numbers(m.get("category") or "")
    for ex in m.get("examples") or []:
        allowed += _numbers(ex.get("title") or "")
    return all(any(n == a or round(a) == n for a in allowed) for n in _numbers(text))
