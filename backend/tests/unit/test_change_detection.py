from datetime import UTC, datetime, timedelta
from decimal import Decimal

from competitor_moves.services import change_detection as cd

NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)
WINDOW = NOW - timedelta(days=7)


def item(**kw):
    base = {"url": "https://s/p/1", "title": "Oval Ring", "category": "Rings", "price": Decimal(100),
            "compare_at_price": None, "currency": "USD", "in_stock": True, "source_date": None}
    return {**base, **kw}


def stored(**kw):
    return {"id": 7, **item(**kw)}


def types(events):
    return sorted(e["type"] for e in events)


def ev(old, new, full=False):
    return cd.product_events(old, new, now=NOW, window_start=WINDOW, prev_full_coverage=full)


def test_first_sight_new_only_when_the_site_dates_it_inside_the_window():
    e = ev(None, item(source_date=NOW - timedelta(days=2)))
    assert types(e) == ["new_product"] and e[0]["occurred_at"] == NOW - timedelta(days=2)
    assert e[0]["after"] == {"title": "Oval Ring", "price": 100.0, "currency": "USD"}
    assert ev(None, item(source_date=NOW - timedelta(days=30))) == []


def test_sampled_site_does_not_report_old_products_as_new():
    assert ev(None, item(), full=False) == []           # no date, partial coverage last time: baseline
    assert types(ev(None, item(), full=True)) == ["new_product"]  # we saw everything last time, so it is new


def test_on_sale_counts_on_first_sight_and_only_when_it_starts():
    e = ev(None, item(price=Decimal(80), compare_at_price=Decimal(100)))
    assert types(e) == ["on_sale"] and e[0]["after"]["pct_off"] == 20.0
    assert ev(stored(price=Decimal(80), compare_at_price=Decimal(100)), item(price=Decimal(80), compare_at_price=Decimal(100))) == []
    assert types(ev(stored(), item(price=Decimal(90), compare_at_price=Decimal(120)))) == ["on_sale", "price_drop"]


def test_price_moves_with_percent():
    [e] = ev(stored(price=Decimal("1299.00")), item(price=Decimal("999.00")))
    assert e["type"] == "price_drop" and e["before"]["price"] == 1299.0 and e["after"]["price"] == 999.0
    assert e["after"]["pct"] == -23.1 and e["occurred_at"] == NOW
    assert types(ev(stored(price=Decimal(100)), item(price=Decimal(115)))) == ["price_rise"]
    assert ev(stored(), item()) == []


def test_missing_price_is_not_a_change():
    assert ev(stored(price=Decimal(100)), item(price=None)) == []
    assert ev(None, item(price=None, source_date=WINDOW - timedelta(days=1))) == []


def test_stock_flips():
    assert types(ev(stored(in_stock=True), item(in_stock=False))) == ["out_of_stock"]
    assert types(ev(stored(in_stock=False), item(in_stock=True))) == ["back_in_stock"]
    assert ev(stored(in_stock=True), item(in_stock=None)) == []  # unknown is not a flip


def test_merge_keeps_known_values_when_the_new_read_lacks_them():
    merged = cd.merge(stored(price=Decimal(100), in_stock=True, category="Rings"), item(price=None, in_stock=None, category=None))
    assert merged["price"] == Decimal(100) and merged["in_stock"] is True and merged["category"] == "Rings"


def test_hash_ignores_irrelevant_fields_and_catches_relevant_ones():
    assert cd.product_hash(item()) == cd.product_hash(item(category="Other", source_date=NOW))
    assert cd.product_hash(item()) != cd.product_hash(item(price=Decimal(101)))
    assert cd.product_hash(item(price=Decimal(100))) == cd.product_hash(item(price=Decimal("100.00")))


def test_sku_normalisation():
    assert cd.norm_sku(" AB-12 x ") == cd.norm_sku("ab12X") == "ab12x"
    assert cd.norm_sku(None) is None and cd.norm_sku(" - ") is None


HOME = {"title": "Lumen", "h1": "Fine jewellery", "promotions": ["Free shipping over $500"],
        "nav": [{"name": "Rings", "url": "https://s/c/rings"}, {"name": "Earrings", "url": "https://s/c/earrings"}]}


def test_home_first_run_is_baseline_then_changes_are_events():
    assert cd.home_events(None, HOME, now=NOW) == []
    new = {**HOME, "promotions": ["20% off lab-grown rings"],
           "nav": HOME["nav"] + [{"name": "Men's Rings", "url": "https://s/c/mens"}]}
    e = cd.home_events(HOME, new, now=NOW)
    assert types(e) == ["new_category", "page_changed", "promo_ended", "promo_started"]
    by = {x["type"]: x for x in e}
    assert by["promo_started"]["after"] == {"text": "20% off lab-grown rings"}
    assert by["new_category"]["category"] == "Men's Rings" and by["new_category"]["after"]["url"] == "https://s/c/mens"
    assert by["page_changed"]["before"]["promotions"] == ["Free shipping over $500"]
    assert cd.home_events(HOME, dict(HOME), now=NOW) == []


def test_landing_pages_and_page_events():
    assert cd.is_landing_page("https://s/pages/about-us", "https://s/sitemap_pages_1.xml")
    assert cd.is_landing_page("https://s/collections/lab-grown", "https://s/sitemap.xml")
    assert cd.is_landing_page("https://s/black-friday", "https://s/sitemap-landing.xml")
    assert not cd.is_landing_page("https://s/products/ring", "https://s/sitemap_pages_1.xml")
    assert not cd.is_landing_page("https://s/ring-123.html", "https://s/sitemap.xml")
    assert cd.page_events(set(), {"https://s/pages/a"}, complete=True, first_run=True, now=NOW) == []
    e = cd.page_events({"https://s/pages/a", "https://s/pages/b"}, {"https://s/pages/a", "https://s/pages/c"},
                       complete=True, first_run=False, now=NOW)
    assert [(x["type"], x["after"] or x["before"]) for x in e] == [
        ("new_page", {"path": "/pages/c", "url": "https://s/pages/c"}), ("page_removed", {"path": "/pages/b", "url": "https://s/pages/b"})]
    e = cd.page_events({"https://s/pages/b"}, set(), complete=False, first_run=False, now=NOW)
    assert e == []  # an incomplete sitemap read can't prove a page is gone


def test_detail_edits_are_logged_as_product_updated():
    [e] = ev(stored(title="Oval Ring", sku="R1", image="https://cdn/x.jpg?v=1"),
             item(title="Oval Halo Ring", sku="R1", image="https://cdn/x.jpg?v=2"))
    assert e["type"] == "product_updated"
    assert e["before"] == {"title": "Oval Ring"} and e["after"] == {"title": "Oval Halo Ring"}  # image query ignored
    assert ev(stored(brand="Lumen"), item(brand=None)) == []  # a missing value is not an edit


def test_sort_orders_put_unknown_values_last():
    from competitor_moves.services.site_crawl import order
    ps = [item(url="a", price=Decimal(50), source_date=None),
          item(url="b", price=None, source_date=NOW - timedelta(days=1)),
          item(url="c", price=Decimal(20), compare_at_price=Decimal(40), source_date=NOW - timedelta(days=9)),
          item(url="d", price=Decimal(90), compare_at_price=Decimal(100), source_date=NOW)]
    assert [p["url"] for p in order(ps, "price_asc")] == ["c", "a", "d", "b"]
    assert [p["url"] for p in order(ps, "price_desc")] == ["d", "a", "c", "b"]
    assert [p["url"] for p in order(ps, "newest")] == ["d", "b", "c", "a"]
    assert [p["url"] for p in order(ps, "discount")] == ["c", "d", "a", "b"]
    assert [p["url"] for p in order(ps, "relevance")] == ["a", "b", "c", "d"]
