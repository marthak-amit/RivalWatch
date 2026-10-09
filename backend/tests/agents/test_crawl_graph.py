"""The crawl agent end to end: local mock stores, fake LLMs, real database."""
import json
from datetime import UTC, datetime, timedelta

from psycopg import sql

from competitor_moves.agents.crawl.graph import run_site_crawl
from competitor_moves.agents.crawl.prompts import RunCopy
from competitor_moves.db.site_schema import table
from tests.agents.fakes import FakeSummarizer, ScriptedResearcher, call, done

NOW = datetime.now(UTC)
JSON = {"content-type": "application/json"}


def iso(days_ago):
    return (NOW - timedelta(days=days_ago)).isoformat()


def product(handle, price, *, days_ago=60, compare=None, kind="Rings", available=True):
    return {"title": handle.replace("-", " ").title(), "handle": handle, "product_type": kind, "vendor": "Lumen",
            "published_at": iso(days_ago),
            "variants": [{"sku": handle.upper(), "price": str(price), "compare_at_price": compare and str(compare),
                          "available": available}]}


def shopify(site, products, collections=(), promo="Free shipping over $500"):
    site.routes["/robots.txt"] = (200, {"content-type": "text/plain"}, "User-agent: *\nDisallow: /checkout\n")
    site.routes["/"] = (200, {}, ('<html><head><title>Lumen</title><script src="https://cdn.shopify.com/s/t.js"></script>'
                                  '</head><body><header><nav><a href="/collections/rings">Rings</a>'
                                  f'<a href="/collections/earrings">Earrings</a></nav></header><h1>Fine jewellery</h1><p>{promo}</p>'
                                  "</body></html>"))
    site.routes["/products.json?limit=250&page=1"] = (200, JSON, json.dumps({"products": products}))
    site.routes["/collections.json?limit=250"] = (200, JSON, json.dumps({"collections": list(collections)}))
    site.routes["/meta.json"] = (200, JSON, json.dumps({"name": "Lumen", "currency": "GBP"}))
    for p in products:  # each product's own page, for its meta tags
        site.routes[f"/products/{p['handle']}"] = (200, {}, (f'<html><head><title>{p["title"]} | Lumen</title>'
                                                   f'<meta name="description" content="Shop {p["title"]}.">'
                                                   f'<meta property="og:title" content="{p["title"]}"></head></html>'))


def rows(conn, site_id, name, where="true"):
    return conn.execute(sql.SQL("select * from {} where " + where + " order by 1").format(table(site_id, name))).fetchall()


def run(conn, sid, **kw):
    kw.setdefault("researcher_llm", None)
    kw.setdefault("summarize_llm", None)
    return run_site_crawl(conn, sid, retry_delay=0, **kw)


CATALOG = [product("oval-halo-ring", 750, days_ago=2), product("pave-band", 80, compare=100), product("stud-earrings", 300, kind="Earrings")]
NEW_COLLECTION = [{"title": "Lab-Grown", "handle": "lab-grown", "published_at": iso(1)},
                  {"title": "Classics", "handle": "classics", "published_at": iso(400)}]


def test_fallback_first_run_on_a_shopify_store(site, make_site, conn):
    shopify(site, CATALOG, NEW_COLLECTION)
    sid = make_site(site.base + "/", owner_email="owner@test.local", credits=50)
    r = run(conn, sid)
    assert r["status"] == "ok" and r["products"] == 3
    events = {(e["type"], e["category"]) for e in rows(conn, sid, "history")}
    assert events == {("new_product", "Rings"), ("on_sale", "Rings"), ("new_category", "Lab-Grown")}  # homepage = baseline
    [crawl] = rows(conn, sid, "crawl_runs")
    assert crawl["full_coverage"] and crawl["est_total_products"] == 3 and crawl["run_type"] == "first"
    # robots, home, feed, meta.json, collections, then one page per product for its meta tags
    assert crawl["pages_fetched"] == 8 and crawl["prompt_version"] == "crawl-v2"
    assert [t["tool"] for t in crawl["trace"] if "tool" in t] == ["inspect_site", "fetch_product_feed"]
    assert conn.execute("select credits from users where email='owner@test.local'").fetchone()["credits"] == 42
    assert {p["currency"] for p in rows(conn, sid, "products")} == {"GBP"}  # from /meta.json
    site_row = conn.execute("select platform, last_run_at, status from sites where id=%s", (sid,)).fetchone()
    assert site_row["platform"] == "shopify" and site_row["last_run_at"] and site_row["status"] == "active"
    moves = {m["headline"] for m in conn.execute("select headline from moves where site_id=%s", (sid,)).fetchall()}
    assert moves == {"Launched 1 product in Rings", "Cut prices on 1 product in Rings, 20% off on average",
                     "Opened a new category: Lab-Grown"}


def test_second_run_without_changes_reuses_ai_text_and_makes_no_llm_call(site, make_site, conn):
    shopify(site, CATALOG, NEW_COLLECTION)
    sid = make_site(site.base + "/")

    def copy(prompt):
        facts = json.loads(prompt.split("<facts>")[1].split("</facts>")[0])
        return RunCopy(moves=[{"key": m["key"], "headline": f"AI: {m['type']} ({m['size']['count']})", "why_it_matters": ""}
                              for m in facts["moves"]], notable=[], run_summary="Lumen launched and discounted rings.")
    first = FakeSummarizer(copy)
    assert run(conn, sid, summarize_llm=first)["summary"] == "Lumen launched and discounted rings."
    assert len(first.calls) == 1 and '"our_categories": []' in first.calls[0]
    second = FakeSummarizer(copy)
    r = run(conn, sid, summarize_llm=second)
    assert second.calls == []  # nothing changed: no model call
    assert r["summary"].startswith(f"No changes on {site.base.removeprefix('http://')} since ")
    heads = {m["headline"] for m in conn.execute("select headline from moves where site_id=%s", (sid,)).fetchall()}
    assert heads == {"AI: launched_products (1)", "AI: discounting (1)", "AI: new_category (1)"}  # kept from run 1


def test_price_drop_becomes_an_event_and_ai_numbers_are_checked(site, make_site, conn):
    shopify(site, CATALOG)
    sid = make_site(site.base + "/")
    run(conn, sid)
    shopify(site, [CATALOG[0], CATALOG[1], product("stud-earrings", 240, kind="Earrings")], promo="20% off earrings this week")
    reply = RunCopy(moves=[{"key": "discounting|Earrings", "headline": "Cut earring prices by 20%",
                            "why_it_matters": "Matches 99 of your products."}],
                    notable=[{"headline": "New homepage promotion", "before": "Free shipping over $500",
                              "after": "20% off earrings this week"}],
                    run_summary="Earrings cut by 20%.")
    summarizer = FakeSummarizer(reply)
    r = run(conn, sid, summarize_llm=summarizer)
    types = sorted(e["type"] for e in rows(conn, sid, "history", "run_id = (select max(id) from {})".replace(
        "{}", f"site_{sid}.crawl_runs")))
    assert types == ["page_changed", "price_drop", "promo_ended", "promo_started"]
    assert "facts" not in r and r["summary"] == "Earrings cut by 20%."
    m = conn.execute("select headline, why_it_matters, size from moves where site_id=%s and category='Earrings'", (sid,)).fetchone()
    assert m["headline"] == "Cut earring prices by 20%" and m["size"]["avg_pct_off"] == 20.0
    assert m["why_it_matters"] is None  # "99" is not in the facts: dropped
    notable = conn.execute("select headline, ai_observed, evidence from moves where site_id=%s and type='notable'", (sid,)).fetchone()
    assert notable["ai_observed"] and notable["evidence"][0]["after"] == "20% off earrings this week"
    [ph] = [h for h in rows(conn, sid, "price_history") if float(h["price"]) == 240.0]
    assert ph["hash"]


def test_researcher_drives_the_tools_and_stops(site, make_site, conn):
    shopify(site, CATALOG)
    sid = make_site(site.base + "/", max_products=150)
    researcher = ScriptedResearcher(call("inspect_site"), call("fetch_product_feed"), done("DONE: feed read, 3 of 150 products"))
    r = run(conn, sid, researcher_llm=researcher)
    assert r["status"] == "ok" and len(researcher.calls) == 3
    system = researcher.calls[0][0].content
    assert f"Domain: {site.base.removeprefix('http://')}" in system and "Product limit for this run: 150 products" in system
    assert "Run type: first" in system and {t.name for t in researcher.tools} >= {"inspect_site", "read_sitemap"}
    tool_msgs = [m for m in researcher.calls[2] if m.type == "tool"]
    assert tool_msgs[1].content.startswith("feed read: 3 products in 1 request(s), kept 3 (site order) (whole catalog)")
    assert tool_msgs[1].content.splitlines()[-1].startswith("status: pages 8/400 | products 3/150")  # + 3 product pages for meta tags
    [crawl] = rows(conn, sid, "crawl_runs")
    assert crawl["trace"][-1] == {"node": "researcher", "final": "DONE: feed read, 3 of 150 products"}


def test_model_failure_falls_back_to_the_fixed_plan(site, make_site, conn):
    shopify(site, CATALOG)
    sid = make_site(site.base + "/")
    r = run(conn, sid, researcher_llm=ScriptedResearcher(RuntimeError("quota"), RuntimeError("quota")),
            summarize_llm=FakeSummarizer(RuntimeError("boom")))
    assert r["status"] == "ok" and r["products"] == 3
    [crawl] = rows(conn, sid, "crawl_runs")
    nodes = [t.get("node") for t in crawl["trace"]]
    assert "fallback_plan" in nodes and any("quota" in t.get("error", "") for t in crawl["trace"])
    assert crawl["summary"] == "Launched 1 product in Rings; Cut prices on 1 product in Rings, 20% off on average"


def test_max_steps_stops_a_looping_researcher(site, make_site, conn):
    shopify(site, CATALOG)
    sid = make_site(site.base + "/")
    researcher = ScriptedResearcher(*[call("inspect_site") for _ in range(10)])
    run(conn, sid, researcher_llm=researcher, max_steps=3)
    assert len(researcher.calls) == 4
    [crawl] = rows(conn, sid, "crawl_runs")
    results = [t["result"] for t in crawl["trace"] if t.get("tool") == "inspect_site"]
    assert len(results) == 3 and results[1] == "ERROR: inspect_site was already called this run"


def test_bad_tool_arguments_and_unknown_categories_come_back_as_errors(site, make_site, conn):
    shopify(site, CATALOG)
    sid = make_site(site.base + "/")
    researcher = ScriptedResearcher(call("inspect_site"), call("fetch_queued_products", source="everything"),
                                    call("fetch_category_listing", category="Watches"), call("no_such_tool"), done())
    run(conn, sid, researcher_llm=researcher)
    msgs = [m.content for m in researcher.calls[-1] if m.type == "tool"]
    assert msgs[1].startswith("ERROR: invalid arguments for fetch_queued_products")
    assert msgs[2].startswith('ERROR: unknown category "Watches". Copy a name from inspect_site, e.g. "Rings", "Earrings"')
    assert msgs[3] == "ERROR: unknown tool no_such_tool"


def test_product_limit_and_page_budget(site, make_site, conn):
    shopify(site, CATALOG)
    sid = make_site(site.base + "/", max_products=2)
    run(conn, sid)
    [crawl] = rows(conn, sid, "crawl_runs")
    assert crawl["products_seen"] == 2 and crawl["limit_hit"] and not crawl["full_coverage"]
    assert crawl["stop_reason"] == "LIMIT REACHED"
    sid2 = make_site(site.base + "/", page_budget=2)
    r = run(conn, sid2)
    [crawl] = rows(conn, sid2, "crawl_runs")
    assert crawl["stop_reason"] == "BUDGET REACHED" and crawl["pages_fetched"] == 2 and r["products"] == 0


def test_credits_running_out_makes_the_run_partial(site, make_site, conn):
    shopify(site, CATALOG)
    sid = make_site(site.base + "/", owner_email="poor@test.local", credits=3)
    r = run(conn, sid)
    assert r["status"] == "partial" and r["pages"] == 3
    [crawl] = rows(conn, sid, "crawl_runs")
    assert crawl["stop_reason"] == "NO CREDITS"
    assert run(conn, sid)["status"] == "skipped"  # 0 credits now: skipped before fetching anything


def test_robots_disallow_all_marks_the_site_blocked(site, make_site, conn):
    site.routes["/robots.txt"] = (200, {"content-type": "text/plain"}, "User-agent: *\nDisallow: /\n")
    sid = make_site(site.base + "/")
    r = run(conn, sid)
    assert r["status"] == "blocked" and "/" not in site.hits
    assert conn.execute("select status from sites where id=%s", (sid,)).fetchone()["status"] == "blocked"
    assert run(conn, sid)["status"] == "skipped"


def test_product_removed_after_two_full_reads_without_it(site, make_site, conn):
    shopify(site, CATALOG)
    sid = make_site(site.base + "/")
    run(conn, sid)
    shopify(site, CATALOG[:2])
    run(conn, sid)
    assert not rows(conn, sid, "history", "type='product_removed'")
    run(conn, sid)
    [removed] = rows(conn, sid, "history", "type='product_removed'")
    assert removed["before"]["title"] == "Stud Earrings"


def sitemap_store(site, *, extra_landing=(), ring_price="1299.00", ring_lastmod=None):
    ns = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'
    site.routes["/robots.txt"] = (200, {"content-type": "text/plain"},
                                  f"User-agent: *\nDisallow: /p/secret\nSitemap: {site.base}/sitemap.xml\n")
    site.routes["/"] = (200, {}, '<html><head><title>Gems</title></head><body><nav><a href="/c/rings">Rings</a></nav></body></html>')
    site.routes["/sitemap.xml"] = (200, {"content-type": "application/xml"},
                                   (f"<sitemapindex {ns}><sitemap><loc>{site.base}/sitemap-products.xml</loc></sitemap>"
                                   f"<sitemap><loc>{site.base}/sitemap-pages.xml</loc></sitemap></sitemapindex>"))
    urls = [("/p/ring", ring_lastmod or iso(1)), ("/p/pendant", iso(90)), ("/p/secret", iso(1))]
    site.routes["/sitemap-products.xml"] = (200, {"content-type": "application/xml"}, f"<urlset {ns}>" + "".join(
        f"<url><loc>{site.base}{u}</loc><lastmod>{d}</lastmod></url>" for u, d in urls) + "</urlset>")
    site.routes["/sitemap-pages.xml"] = (200, {"content-type": "application/xml"}, f"<urlset {ns}>" + "".join(
        f"<url><loc>{site.base}{u}</loc></url>" for u in ["/pages/about", *extra_landing]) + "</urlset>")
    ld = '<script type="application/ld+json">{{"@type":"Product","name":"{n}","sku":"{s}","offers":{{"price":"{p}","priceCurrency":"USD"}}}}</script>'
    site.routes["/p/ring"] = (200, {}, "<html><head>" + ld.format(n="1 ct Oval Lab-Grown Ring", s="G-1", p=ring_price) + "</head></html>")
    site.routes["/p/pendant"] = (200, {}, "<html><head>" + ld.format(n="Pearl Pendant", s="G-2", p="450") + "</head></html>")


def test_generic_store_through_sitemaps_and_json_ld(site, make_site, conn):
    sitemap_store(site)
    sid = make_site(site.base + "/")
    r = run(conn, sid)
    assert r["status"] == "ok" and r["products"] == 2 and "/p/secret" not in site.hits
    prods = {p["sku"]: p for p in rows(conn, sid, "products")}
    assert prods["G-1"]["attributes"]["stone"] == "lab_grown" and float(prods["G-1"]["price"]) == 1299.0
    assert {e["type"] for e in rows(conn, sid, "history")} == {"new_product"}  # ring's lastmod is recent; pendant is old
    assert [p["url"] for p in rows(conn, sid, "pages")] == [f"{site.base}/pages/about"]
    sitemap_store(site, extra_landing=["/pages/black-friday"], ring_price="999.00", ring_lastmod=iso(0))
    run(conn, sid)
    latest = rows(conn, sid, "history", f"run_id = (select max(id) from site_{sid}.crawl_runs)")
    assert sorted(e["type"] for e in latest) == ["new_page", "price_drop"]
    [crawl] = rows(conn, sid, "crawl_runs", "run_type = 'incremental'")
    assert [t["tool"] for t in crawl["trace"] if "tool" in t] == [
        "inspect_site", "read_sitemap", "fetch_queued_products", "recheck_known_products"]


def test_robots_rules_added_later_are_respected_on_recheck(site, make_site, conn):
    sitemap_store(site)
    sid = make_site(site.base + "/")
    run(conn, sid)
    assert "/p/pendant" in site.hits
    site.hits.clear()
    site.routes["/robots.txt"] = (200, {"content-type": "text/plain"},
                                  f"User-agent: *\nDisallow: /p/\nSitemap: {site.base}/sitemap.xml\n")
    run(conn, sid)
    assert not [h for h in site.hits if h.startswith("/p/")]  # stored products under /p/ are no longer fetched


def test_three_refusals_in_a_row_mark_the_site_blocked(site, make_site, conn):
    sitemap_store(site)
    for path in ("/p/ring", "/p/pendant"):
        site.routes[path] = (403, {}, "Forbidden")
    site.routes["/sitemap-products.xml"] = (200, {"content-type": "application/xml"},
                                            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + "".join(
                                                f"<url><loc>{site.base}/p/{n}</loc></url>" for n in ("ring", "pendant", "a", "b"))
                                            + "</urlset>")
    site.routes["/p/a"] = site.routes["/p/b"] = (429, {}, "Too Many Requests")
    sid = make_site(site.base + "/")
    r = run(conn, sid)
    assert r["status"] == "blocked"
    [crawl] = rows(conn, sid, "crawl_runs")
    assert crawl["stop_reason"] == "BLOCKED" and crawl["pages_failed"] == 3
    assert conn.execute("select status from sites where id=%s", (sid,)).fetchone()["status"] == "blocked"


def test_product_limit_does_not_stop_other_signals(site, make_site, conn):
    shopify(site, CATALOG, NEW_COLLECTION)
    sid = make_site(site.base + "/", max_products=1)
    run(conn, sid)
    assert {e["type"] for e in rows(conn, sid, "history")} >= {"new_category"}  # collections still checked
    [crawl] = rows(conn, sid, "crawl_runs")
    assert crawl["products_seen"] == 1 and crawl["limit_hit"] and crawl["stop_reason"] == "LIMIT REACHED"


def test_where_each_value_came_from_is_stored_in_plain_columns(site, make_site, conn):
    shopify(site, CATALOG)
    sid = make_site(site.base + "/")
    run(conn, sid)
    p = rows(conn, sid, "products", "path = '/products/stud-earrings'")[0]
    assert p["source_url"] == f"{site.base}/products.json?limit=250&page=1" and p["currency"] == "GBP"
    shopify(site, [CATALOG[0], CATALOG[1], product("stud-earrings", 240, kind="Earrings")], promo="20% off earrings")
    run(conn, sid)
    [drop] = rows(conn, sid, "history", "type = 'price_drop'")
    assert (drop["title"], drop["path"], drop["currency"], float(drop["price_before"]), float(drop["price_after"])) == (
        "Stud Earrings", "/products/stud-earrings", "GBP", 300.0, 240.0)
    assert drop["url"] == f"{site.base}/products/stud-earrings" and drop["source_url"].endswith("/products.json?limit=250&page=1")
    [promo] = rows(conn, sid, "history", "type = 'promo_started'")
    assert promo["title"] == "20% off earrings" and promo["source_url"] == site.base + "/" and promo["product_id"] is None


def test_product_page_gone_twice_is_removed_and_linked_to_its_product(site, make_site, conn):
    sitemap_store(site)
    sid = make_site(site.base + "/")
    run(conn, sid)
    site.routes["/p/pendant"] = (404, {}, "gone")
    run(conn, sid)
    run(conn, sid)
    [removed] = rows(conn, sid, "history", "type = 'product_removed'")
    pendant = rows(conn, sid, "products", "path = '/p/pendant'")[0]
    assert removed["product_id"] == pendant["id"] and pendant["removed_at"] is not None
    assert removed["path"] == "/p/pendant" and removed["title"] == "Pearl Pendant" and float(removed["price_before"]) == 450.0
    assert rows(conn, sid, "products", "path = '/p/ring'")[0]["source_url"] == f"{site.base}/p/ring"  # read from its own page


RINGS = [product(f"ring-{i}", p, days_ago=d, compare=c) for i, (p, d, c) in enumerate(
    [(500, 30, None), (120, 1, None), (900, 3, 1200), (300, 90, 310), (60, 400, None)])]


def shopify_collections(site, collections: dict[str, list]):
    """Per-category feeds, as Shopify serves them: /collections/<handle>/products.json."""
    for handle, items in collections.items():
        site.routes[f"/collections/{handle}/products.json?limit=250&page=1"] = (200, JSON, json.dumps({"products": items}))


def test_scope_reads_only_the_chosen_category_and_sort_picks_which_products(site, make_site, conn):
    shopify(site, CATALOG)
    shopify_collections(site, {"rings": RINGS, "earrings": [product("hoop", 50, kind="Earrings")]})
    cases = {"price_asc": {"ring-4", "ring-1"}, "price_desc": {"ring-2", "ring-0"}, "newest": {"ring-1", "ring-2"},
             "discount": {"ring-2", "ring-3"}, "relevance": {"ring-0", "ring-1"}}
    for sort, expected in cases.items():
        sid = make_site(site.base + "/", max_products=2, sort=sort,
                        categories=json.dumps([{"name": "Rings", "url": f"{site.base}/collections/rings"}]))
        run(conn, sid)
        got = {p["path"].rsplit("/", 1)[-1] for p in rows(conn, sid, "products")}
        assert got == expected, sort
        assert all(p["source_url"].endswith("/collections/rings/products.json?limit=250&page=1") for p in rows(conn, sid, "products"))
    assert "/collections/earrings/products.json?limit=250&page=1" not in site.hits  # outside the scope: never read
    [crawl] = rows(conn, sid, "crawl_runs")
    assert not crawl["full_coverage"]  # a scoped read never proves anything about the rest of the site


def test_scope_is_enforced_on_tools_and_shown_to_the_researcher(site, make_site, conn):
    sitemap_store(site)
    site.routes["/"] = (200, {}, '<html><body><nav><a href="/c/rings">Rings</a><a href="/c/watches">Watches</a></nav></body></html>')
    site.routes["/c/rings"] = (200, {}, '<html><body><a href="/p/ring">Ring</a></body></html>')
    sid = make_site(site.base + "/", categories=json.dumps([{"name": "Rings", "url": f"{site.base}/c/rings"}]), sort="newest")
    researcher = ScriptedResearcher(call("inspect_site"), call("fetch_category_listing", category="Watches"),
                                    call("fetch_category_listing", category="Rings"), call("fetch_queued_products", source="all"),
                                    done())
    run(conn, sid, researcher_llm=researcher)
    system = researcher.calls[0][0].content
    assert 'Scope: only these menu categories: "Rings"' in system and "Which products fill the limit first: newest first" in system
    msgs = [m.content for m in researcher.calls[-1] if m.type == "tool"]
    assert msgs[1].startswith('ERROR: "Watches" is outside this site\'s scope')
    assert {p["path"] for p in rows(conn, sid, "products")} == {"/p/ring"}  # pendant is not in the Rings listing


def test_scope_without_a_feed_uses_the_category_listings_in_the_fixed_plan(site, make_site, conn):
    sitemap_store(site)
    site.routes["/c/rings"] = (200, {}, '<html><body><a href="/p/ring">Ring</a></body></html>')
    sid = make_site(site.base + "/", categories=json.dumps([{"name": "Rings", "url": f"{site.base}/c/rings"}]))
    run(conn, sid)
    assert {p["path"] for p in rows(conn, sid, "products")} == {"/p/ring"}
    assert "/p/pendant" not in site.hits


def test_feed_products_get_their_page_meta_and_meta_edits_are_history(site, make_site, conn):
    shopify(site, CATALOG)
    sid = make_site(site.base + "/")
    run(conn, sid)
    p = rows(conn, sid, "products", "path = '/products/pave-band'")[0]
    assert p["meta_title"] == "Pave Band | Lumen" and p["meta_description"] == "Shop Pave Band."
    assert p["meta"] == {"description": "Shop Pave Band.", "og:title": "Pave Band"} and p["meta_fetched_at"]
    site.routes["/products/pave-band"] = (200, {}, ('<html><head><title>Pavé Band – New Season | Lumen</title>'
                                                   '<meta name="description" content="Shop Pave Band."></head></html>'))
    run(conn, sid)
    assert not rows(conn, sid, "history", "type = 'product_updated'")  # meta re-read only when stale
    conn.execute(f"update site_{sid}.products set meta_fetched_at = now() - interval '8 days'")
    run(conn, sid)
    [upd] = rows(conn, sid, "history", "type = 'product_updated'")
    assert upd["before"] == {"meta_title": "Pave Band | Lumen"} and upd["after"] == {"meta_title": "Pavé Band – New Season | Lumen"}
    assert upd["path"] == "/products/pave-band"


def plain_store(site, pages: dict[str, str], *, lastmod=None):
    """A store with no feed and no structured data: products only as visible HTML (like aura_jewels)."""
    ns = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'
    site.routes["/robots.txt"] = (200, {"content-type": "text/plain"}, f"User-agent: *\nSitemap: {site.base}/sitemap.xml\n")
    site.routes["/"] = (200, {}, '<html><body><nav><a href="/category/ring">Ring</a></nav><h1>Aura</h1></body></html>')
    site.routes["/sitemap.xml"] = (200, {"content-type": "application/xml"}, f"<urlset {ns}>" + "".join(
        f"<url><loc>{site.base}{u}</loc>{f'<lastmod>{lastmod}</lastmod>' if lastmod else ''}</url>" for u in pages) + "</urlset>")
    for u, html in pages.items():
        site.routes[u] = (200, {}, html)


def test_pages_without_structured_data_are_read_from_their_html(site, make_site, conn):
    plain_store(site, {"/product/bangle-20": "<html><body><a href='/'>Home</a><h1>Bangle Wristband 20</h1>"
                                             "<p>SKU: BRC-0020</p><p>₹2,223.16</p><p>Elegant bangle.</p></body></html>"})
    sid = make_site(site.base + "/")
    run(conn, sid)
    [p] = rows(conn, sid, "products")
    assert (p["title"], float(p["price"]), p["currency"], p["sku"], p["extracted_by"]) == (
        "Bangle Wristband 20", 2223.16, "INR", "BRC-0020", "html")


TEMPLATE_PAGE = """<html><head><script type="application/ld+json">{"@type":"Product","name":"{{nj_pdp_name}}"}</script></head>
<body><h1>{{nj_pdp_name}}</h1><script>setTimeout(() => {
  document.querySelector('h1').textContent = 'Lakshmi Necklace';
  document.querySelector('script[type="application/ld+json"]').textContent =
    JSON.stringify({"@type": "Product", "name": "Lakshmi Necklace", "sku": "GT-1", "offers": {"price": "797204", "priceCurrency": "INR"}});
}, 100);</script></body></html>"""


def test_javascript_pages_are_rendered_when_the_browser_is_on(site, make_site, conn):
    plain_store(site, {"/p/lakshmi": TEMPLATE_PAGE})
    off = make_site(site.base + "/", crawl_settings=json.dumps({"browser": "off"}))
    run(conn, off)
    assert rows(conn, off, "products") == []  # template values are never stored
    [crawl] = rows(conn, off, "crawl_runs")
    assert any("needs browser 1" in t.get("result", "") for t in crawl["trace"])
    on = make_site(site.base + "/", crawl_settings=json.dumps({"browser": "auto"}))
    run(conn, on)
    [crawl] = rows(conn, on, "crawl_runs")
    if any("no browser available" in t.get("error", "") for t in crawl["trace"]):
        import pytest
        pytest.skip("no browser on this machine")
    [p] = rows(conn, on, "products")
    assert (p["title"], float(p["price"]), p["sku"], p["extracted_by"]) == ("Lakshmi Necklace", 797204.0, "GT-1", "jsonld+browser")
    assert crawl["pages_fetched"] == 5  # robots, home, sitemap, the raw page, and the rendered page


class FakeExtractLLM:
    """Stands in for the extract model: answers with a fixed ExtractedProduct."""

    def __init__(self, **reply):
        self.reply, self.calls = reply, 0

    def with_structured_output(self, schema):
        self.schema = schema
        return self

    def invoke(self, prompt):
        self.calls += 1
        return self.schema(**self.reply)


def test_ai_reads_pages_the_rules_cannot_and_its_answers_are_checked(site, make_site, conn):
    page = ("<html><body><h1>Halo Ring</h1><p>Our bestseller.</p><div>Now only</div><div>Rs. 15,500 incl. GST</div>"
            "<div>EMI from Rs. 1,292</div><a href='/r1'>Ring 1</a><a href='/r2'>Ring 2</a><a href='/r3'>Ring 3</a></body></html>")
    plain_store(site, {"/p/halo": page, "/p/story": "<html><body><h1>Our Story</h1><p>Since 1990.</p></body></html>"})
    good = FakeExtractLLM(is_product=True, title="Halo Ring", price=15500, currency="inr", in_stock=True)
    sid = make_site(site.base + "/")
    run(conn, sid, extract_llm=good)
    [p] = rows(conn, sid, "products")
    assert (p["title"], float(p["price"]), p["currency"], p["extracted_by"], p["compare_at_price"]) == (
        "Halo Ring", 15500.0, "INR", "ai", None)
    liar = FakeExtractLLM(is_product=True, title="Halo Ring", price=9999, currency="INR")  # a price the page never shows
    sid2 = make_site(site.base + "/")
    run(conn, sid2, extract_llm=liar)
    assert {(p["title"], p["price"]) for p in rows(conn, sid2, "products")} == {("Halo Ring", None)}
    capped = make_site(site.base + "/", crawl_settings=json.dumps({"ai_extract_cap": 0}))
    counter = FakeExtractLLM(is_product=True, title="Halo Ring", price=15500)
    run(conn, capped, extract_llm=counter)
    assert counter.calls == 0 and rows(conn, capped, "products") == []


def test_sitemaps_that_date_everything_today_are_not_trusted(site, make_site, conn):
    pages = {f"/product/b-{i}": f"<html><body><h1>Bangle {i}</h1><p>₹{100 + i}.00</p></body></html>" for i in range(25)}
    plain_store(site, pages, lastmod=iso(0))
    sid = make_site(site.base + "/", max_products=25)
    run(conn, sid)
    assert len(rows(conn, sid, "products")) == 25
    assert rows(conn, sid, "history", "type = 'new_product'") == []  # "changed today" on every URL proves nothing
    [crawl] = rows(conn, sid, "crawl_runs")
    assert any("sitemap dates ignored" in t.get("result", "") for t in crawl["trace"])
