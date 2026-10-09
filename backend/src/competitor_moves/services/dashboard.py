"""What the RivalWatch dashboard reads: competitor cards, the change feed, digests and reports.

Shapes follow the UI's own server (server.js, lib/monitor.js, lib/reports.js, lib/digest.js) so the pages work
unchanged. Change types are mapped to the UI's names; types the UI doesn't know keep their own name and a label.
"""
from collections import Counter
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from croniter import croniter
from psycopg import sql

from ..config import DEFAULT_CRON, get_settings
from ..core.plans import PLANS
from ..db.site_schema import table
from . import store_sync, workspaces

SYMBOL = {"USD": "$", "GBP": "£", "EUR": "€", "INR": "₹", "AUD": "A$", "CAD": "C$", "JPY": "¥"}
MAX_CHANGES = 1000
CARD_PRODUCTS = 25  # the card lists the most recently changed products, plus a count


def money(amount, currency: str | None) -> str | None:
    if amount is None:
        return None
    sym = SYMBOL.get(currency or "", f"{currency} " if currency else "")
    return f"{sym}{float(amount):,.2f}"


def _iso(d: datetime | None) -> str | None:
    return d.astimezone(UTC).isoformat().replace("+00:00", "Z") if d else None


# ---- change feed ----------------------------------------------------------------------------------------------

def ui_change(h: dict, site: dict) -> dict:
    t, name, cur = h["type"], h["title"], h["currency"]
    before, after = h["before"] or {}, h["after"] or {}
    c = {"id": f"{site['id']}-{h['id']}", "competitor": site["name"], "competitorId": str(site["id"]),
         "ts": _iso(h["occurred_at"]), "sample": False, "kind": t, "category": h["category"], "url": h["url"]}
    if t == "new_product":
        c.update(signal="product", type="product_added", name=name, price=money(h["price_after"], cur))
    elif t == "product_removed":
        c.update(signal="product", type="product_removed", name=name, price=money(h["price_before"], cur))
    elif t in ("price_drop", "price_rise"):
        c.update(signal="product", type="price_change", name=name, **{"from": money(h["price_before"], cur)},
                 to=money(h["price_after"], cur), pct=after.get("pct"))
    elif t == "on_sale":
        c.update(signal="product", type="on_sale", name=name, price=money(h["price_after"], cur),
                 was=money(after.get("compare_at_price"), cur), pct=-after["pct_off"] if after.get("pct_off") else None)
    elif t in ("out_of_stock", "back_in_stock"):
        c.update(signal="product", type=t, name=name)
    elif t == "product_updated":
        c.update(signal="product", type=t, name=name, fields=sorted(after))
    elif t == "promo_started":
        c.update(signal="page", type="new_promo", text=after.get("text"))
    elif t == "promo_ended":
        c.update(signal="page", type="promo_ended", text=before.get("text"))
    elif t in ("new_page", "page_removed"):
        c.update(signal="page", type=t, path=(after or before).get("path"))
    elif t == "new_category":
        c.update(signal="page", type=t, name=h["category"], path=urlsplit(after.get("url") or "").path or None)
    else:
        c.update(signal="page", type=t)
    c["label"] = describe(c)
    return c


def describe(c: dict) -> str:
    t, n = c["type"], c.get("name")
    pct = c.get("pct")
    pct_s = f" ({'+' if pct and pct > 0 else ''}{pct:g}%)" if pct is not None else ""
    return {
        "price_change": lambda: f"{n}: {c.get('from')} → {c.get('to')}{pct_s}",
        "product_added": lambda: f"New product: {n} at {c.get('price')}",
        "product_removed": lambda: f"Product removed: {n} (was {c.get('price')})",
        "on_sale": lambda: f"On sale: {n} {c.get('price')} (was {c.get('was')}){pct_s}",
        "out_of_stock": lambda: f"Out of stock: {n}",
        "back_in_stock": lambda: f"Back in stock: {n}",
        "product_updated": lambda: f"Updated {', '.join(c.get('fields') or [])}: {n}",
        "new_promo": lambda: f"New promotion: “{c.get('text')}”",
        "promo_ended": lambda: f"Promotion ended: “{c.get('text')}”",
        "new_page": lambda: f"New page: {c.get('path')}",
        "page_removed": lambda: f"Page removed: {c.get('path')}",
        "new_category": lambda: f"New category: {n}",
        "page_changed": lambda: "Homepage changed",
    }.get(t, lambda: t)()


def changes(conn, sites: list[dict], *, since: datetime | None = None, limit: int = MAX_CHANGES) -> list[dict]:
    out = []
    for s in sites:
        rows = conn.execute(sql.SQL(
            "select id, type, category, before, after, occurred_at, title, url, currency, price_before, price_after "
            "from {} where occurred_at >= %s order by occurred_at desc, id desc limit %s").format(table(s["id"], "history")),
            (since or datetime(1970, 1, 1, tzinfo=UTC), limit)).fetchall()
        out += [ui_change(r, s) for r in rows]
    out.sort(key=lambda c: c["ts"], reverse=True)
    return out[:limit]


def crawl_log(conn, sites: list[dict], since: datetime) -> list[dict]:
    out = []
    for s in sites:
        out += [{"ts": _iso(r["finished_at"]), "changes": sum((r["changes"] or {}).values()), "pages": r["pages_fetched"],
                 "competitorId": str(s["id"]), "status": r["status"], "summary": r["summary"]}
                for r in conn.execute(sql.SQL(
                    "select finished_at, changes, pages_fetched, status, summary from {} where finished_at >= %s "
                    "order by id").format(table(s["id"], "crawl_runs")), (since,)).fetchall()]
    return sorted(out, key=lambda r: r["ts"])


# ---- competitor cards -----------------------------------------------------------------------------------------

def competitor_view(conn, site: dict) -> dict:
    s = get_settings()
    runs = conn.execute(sql.SQL("select count(*) filter (where status in ('ok','partial')) as n, "
                                "max(finished_at) filter (where status in ('ok','partial')) as last from {}").format(
        table(site["id"], "crawl_runs"))).fetchone()
    last = conn.execute(sql.SQL("select * from {} where status in ('ok','partial') order by id desc limit 1").format(
        table(site["id"], "crawl_runs"))).fetchone()
    running = conn.execute(sql.SQL("select * from {} where status = 'running' order by id desc limit 1").format(
        table(site["id"], "crawl_runs"))).fetchone()
    if last is None and running and running["products_seen"]:
        last = running  # first crawl still going: show what it has found so far
    snapshot = None
    if last:
        prods = conn.execute(sql.SQL(
            "select title, price, currency, url from {} where removed_at is null order by last_seen_at desc, id desc limit %s"
        ).format(table(site["id"], "products")), (CARD_PRODUCTS,)).fetchall()
        count = conn.execute(sql.SQL("select count(*) as n from {} where removed_at is null").format(
            table(site["id"], "products"))).fetchone()["n"]
        pages = [r["url"] for r in conn.execute(sql.SQL("select url from {} order by first_seen_at desc limit 20").format(
            table(site["id"], "pages"))).fetchall()]
        home = last["home"] or {}
        snapshot = {
            "ts": _iso(last["finished_at"]), "pagesCrawled": last["pages_fetched"],
            "products": {p["url"]: {"name": p["title"], "price": money(p["price"], p["currency"]),
                                    "amount": float(p["price"]) if p["price"] is not None else None} for p in prods},
            "productCount": count, "estTotal": last["est_total_products"],
            "promotions": home.get("promotions") or [],
            "pages": [urlsplit(u).path or "/" for u in pages] or [urlsplit(n["url"]).path or "/" for n in (home.get("nav") or [])[:20]],
            "profile": home.get("profile"), "summary": last["summary"], "inProgress": last["status"] == "running"}
    error = site["last_error"] or ("The site blocks our crawler (robots.txt or repeated refusals)" if site["status"] == "blocked" else None)
    return {"id": str(site["id"]), "name": site["name"] or site["domain"], "url": site["url"], "site": None,
            "enabled": site["status"] != "paused", "status": site["status"], "error": error, "snapshot": snapshot,
            "snapshots": runs["n"], "since": _iso(site["created_at"]), "lastRun": _iso(runs["last"]),
            "crawling": running is not None, "progress": {"pages": running["pages_fetched"], "products": running["products_seen"]}
            if running else None,
            "platform": site["platform"], "maxProducts": min(site["max_products"] or s.default_max_products, s.max_products_cap),
            "pageBudget": site["page_budget"] or s.page_budget, "categories": site["categories"] or [], "sort": site["sort"],
            "cron": site["cron"], "nextRun": _iso(site["next_run_at"]),
            "crawlSettings": workspaces.crawl_settings(site["crawl_settings"])}


# ---- digest (rule-based; the AI analyst replaces it in milestone M6) -------------------------------------------

def rules_digest(chs: list[dict]) -> dict:
    """Port of the UI's viaRules: 3-5 suggested actions from a batch of changes."""
    actions = []

    def add(a):
        if len(actions) < 5:
            actions.append(a)
    by = lambda t: [c for c in chs if c["type"] == t]
    for c in [c for c in by("price_change") if (c.get("pct") or 0) < 0]:
        add({"priority": "high", "title": f"Review our pricing against {c['competitor']}'s {c['name']} cut",
             "why": f"{c['competitor']} dropped {c['name']} {c['from']} → {c['to']} ({c['pct']}%). Decide whether to match, bundle, or differentiate on value."})
    for c in by("on_sale"):
        add({"priority": "high", "title": f"Check {c['competitor']}'s sale on {c['name']}",
             "why": f"Now {c['price']} (was {c['was']}). Consider a time-boxed offer or highlight non-price advantages."})
    for c in [c for c in by("price_change") if (c.get("pct") or 0) > 0]:
        add({"priority": "medium", "title": f"Use {c['competitor']}'s price rise in sales conversations",
             "why": f"{c['name']} went {c['from']} → {c['to']} (+{c['pct']}%). Target their price-sensitive customers with a switching offer."})
    for c in by("new_promo"):
        add({"priority": "high", "title": f"Counter {c['competitor']}'s promotion",
             "why": f"They launched “{c['text']}”. Consider a time-boxed offer or highlight non-price advantages."})
    for c in by("product_added"):
        add({"priority": "medium", "title": f"Assess {c['competitor']}'s new \"{c['name']}\"",
             "why": f"Added at {c['price']}. Check overlap with our catalogue and whether we have a gap."})
    for c in by("new_category"):
        add({"priority": "medium", "title": f"Look at {c['competitor']}'s new category {c['name']}",
             "why": "A new category often signals a push into a segment; check whether we compete there."})
    for c in by("new_page"):
        add({"priority": "low", "title": f"Read {c['competitor']}'s new page {c['path']}",
             "why": "New pages often signal launches or repositioning; review messaging and update battlecards."})
    for c in by("product_removed"):
        add({"priority": "medium", "title": f"Court {c['competitor']}'s {c['name']} customers",
             "why": f"{c['name']} was removed from their site; customers may be looking for alternatives."})
    for t in ("Brief sales and support on these changes this week", "Update the competitor battlecards",
              "Keep monitoring these competitors daily"):
        if len(actions) < 3:
            add({"priority": "low", "title": t, "why": "Keeps the team aligned on competitor movements."})
    names = ", ".join(dict.fromkeys(c["competitor"] for c in chs))
    n = lambda t, one, many: f"{len(by(t))} {one if len(by(t)) == 1 else many}"
    summary = (f"{len(chs)} change{'' if len(chs) == 1 else 's'} detected across {names}: "
               f"{n('price_change', 'price move', 'price moves')}, {n('new_promo', 'new promotion', 'new promotions')}, "
               f"{n('product_added', 'new product', 'new products')}, {n('new_page', 'new page', 'new pages')}.")
    return {"summary": summary, "actions": actions, "source": "rules"}


def digests(chs: list[dict], days: int = 10) -> list[dict]:
    """One digest per day that had changes, newest first (the UI keeps the last 10)."""
    by_day: dict[str, list[dict]] = {}
    for c in chs:
        by_day.setdefault(c["ts"][:10], []).append(c)
    out = []
    for day in sorted(by_day, reverse=True)[:days]:
        batch = by_day[day]
        out.append({"ts": max(c["ts"] for c in batch), "changeCount": len(batch), **rules_digest(batch)})
    return out


# ---- reports (port of lib/reports.js) --------------------------------------------------------------------------

def build_report(chs: list[dict], runs: list[dict], period: str, now: datetime | None = None, competitor_id: str = "") -> dict:
    now = now or datetime.now(UTC)
    days = 30 if period == "month" else 7

    def day_keys(offset: int = 0) -> list[str]:
        return [(now - timedelta(days=days - 1 - i + offset)).strftime("%Y-%m-%d") for i in range(days)]
    keys, prev = day_keys(), set(day_keys(days))
    cur = set(keys)
    chs = [c for c in chs if not competitor_id or c["competitorId"] == competitor_id]
    in_cur = [c for c in chs if c["ts"][:10] in cur]
    prev_total = sum(1 for c in chs if c["ts"][:10] in prev)
    daily = [{"date": d, "product": 0, "page": 0} for d in keys]
    by_date = {d["date"]: d for d in daily}
    by_type, comps = Counter(), {}
    for c in in_cur:
        by_date[c["ts"][:10]][c["signal"]] += 1
        by_type[c["type"]] += 1
        r = comps.setdefault(c["competitorId"], {"id": c["competitorId"], "name": c["competitor"], "total": 0, "product": 0,
                                                 "page": 0, "priceMoves": 0, "promos": 0, "newPages": 0})
        r["total"] += 1
        r[c["signal"]] += 1
        r["priceMoves"] += c["type"] == "price_change"
        r["promos"] += c["type"] == "new_promo"
        r["newPages"] += c["type"] == "new_page"
    by_comp = sorted(comps.values(), key=lambda r: -r["total"])
    moves = sorted([c for c in in_cur if c["type"] == "price_change" and c.get("pct") is not None],
                   key=lambda c: -abs(c["pct"]))[:5]
    moves = [{k: c[k] for k in ("competitor", "name", "from", "to", "pct", "ts")} for c in moves]
    cur_runs = [r for r in runs if r["ts"] and r["ts"][:10] in cur]
    highlights = []
    if moves:
        m = moves[0]
        highlights.append(f"Biggest price move: {m['competitor']} {m['name']} {m['from']} → {m['to']} "
                          f"({'+' if m['pct'] > 0 else ''}{m['pct']}%).")
    if by_comp:
        b = by_comp[0]
        highlights.append(f"Most active competitor: {b['name']} with {b['total']} change{'' if b['total'] == 1 else 's'}.")
    if by_type["new_promo"]:
        highlights.append(f"{by_type['new_promo']} new promotion{'' if by_type['new_promo'] == 1 else 's'} launched.")
    if by_type["new_page"]:
        highlights.append(f"{by_type['new_page']} new page{'' if by_type['new_page'] == 1 else 's'} published.")
    if not in_cur:
        highlights.append("No competitor changes detected in this period.")
    return {
        "period": period, "competitorId": competitor_id, "days": days, "from": keys[0], "to": keys[-1], "total": len(in_cur),
        "prevTotal": prev_total, "deltaPct": round((len(in_cur) - prev_total) / prev_total * 100) if prev_total else None,
        "bySignal": {"product": sum(c["signal"] == "product" for c in in_cur), "page": sum(c["signal"] == "page" for c in in_cur)},
        "byType": dict(by_type), "daily": daily, "byCompetitor": by_comp, "biggestMoves": moves, "highlights": highlights,
        "crawls": {"count": len(cur_runs), "pages": sum(r["pages"] for r in cur_runs),
                   "withChanges": sum(1 for r in cur_runs if r["changes"] > 0)},
        "hasSample": False,
        "changes": [{k: c[k] for k in ("ts", "competitor", "type", "label", "signal", "sample")} for c in in_cur],
    }


# ---- the whole dashboard state ---------------------------------------------------------------------------------

def _interval_sec(cron: str) -> int:
    try:
        it = croniter(cron or DEFAULT_CRON, datetime.now(UTC))
        a, b = it.get_next(datetime), it.get_next(datetime)
        return int((b - a).total_seconds())
    except (ValueError, KeyError):
        return 86400


def state(conn, user: dict) -> dict:
    project = workspaces.get_or_create(conn, user)
    sites = workspaces.competitors(conn, project["id"])
    chs = changes(conn, sites)
    from . import digests as stored  # stored digests import this module, so import here
    digest_list = stored.ui_list(conn, project["id"]) or digests(chs)
    ids = [str(s["id"]) for s in sites]
    running = bool(ids) and conn.execute(
        "select 1 from jobs where type='crawl_site' and status in ('queued','running') and payload->>'site_id' = any(%s) limit 1",
        (ids,)).fetchone() is not None
    active = [s for s in sites if s["status"] == "active"]
    next_runs = [s["next_run_at"] for s in active if s["next_run_at"]]
    interval = _interval_sec(active[0]["cron"]) if active else 86400
    next_run = min(next_runs) if next_runs else datetime.now(UTC) + timedelta(seconds=interval)
    run_counts = [conn.execute(sql.SQL("select count(*) as n, max(finished_at) as last from {} where finished_at is not null")
                               .format(table(s["id"], "crawl_runs"))).fetchone() for s in sites]
    last = max((r["last"] for r in run_counts if r["last"]), default=None)
    credits = conn.execute("select credits, plan from users where id=%s", (user["id"],)).fetchone()
    return {"competitors": [competitor_view(conn, s) for s in sites], "changes": chs,
            "digest": digest_list[0] if digest_list else None, "digests": digest_list,
            "runs": sum(r["n"] for r in run_counts), "lastRun": _iso(last), "running": running,
            "credits": credits["credits"], "plan": PLANS[credits["plan"]],
            "nextRun": int(next_run.timestamp() * 1000), "intervalSec": interval,
            "mode": "gemini" if get_settings().gemini_api_key else "rules", "testSites": [],
            "store": store_sync.store_view(conn, user)}
