from datetime import UTC, datetime

from competitor_moves.services import moves

T = datetime(2026, 10, 8, tzinfo=UTC)


def e(id, type, pid=None, category="Rings", before=None, after=None):
    return {"id": id, "type": type, "product_id": pid, "category": category, "before": before, "after": after or {},
            "occurred_at": T, "title": f"Ring {pid}", "url": f"https://s/p/{pid}"}


def test_grouping_by_type_and_category_with_sizes():
    events = [e(1, "new_product", 1, after={"price": 750, "currency": "USD"}),
              e(2, "new_product", 2, after={"price": 1250, "currency": "USD"}),
              e(3, "new_product", 3, category="Earrings", after={"price": 300, "currency": "USD"}),
              e(4, "on_sale", 4, after={"price": 80, "compare_at_price": 100, "pct_off": 20.0, "currency": "USD"}),
              e(5, "price_drop", 4, before={"price": 90}, after={"price": 80, "pct": -11.1, "currency": "USD"}),
              e(6, "price_drop", 5, before={"price": 100}, after={"price": 70, "pct": -30.0, "currency": "USD"}),
              e(7, "promo_started", None, category=None, after={"text": "20% off rings"}),
              e(8, "page_changed", None, category=None), e(9, "back_in_stock", 9)]
    got = {m["key"]: m for m in moves.group(events)}
    assert set(got) == {"launched_products|Rings", "launched_products|Earrings", "discounting|Rings", "discounting|"}
    launched = got["launched_products|Rings"]
    assert launched["size"] == {"count": 2, "min_price": 750.0, "max_price": 1250.0, "currency": "USD"}
    assert launched["evidence_event_ids"] == [1, 2] and len(launched["examples"]) == 2
    disc = got["discounting|Rings"]
    assert disc["size"]["count"] == 2  # distinct products, not events
    assert disc["size"]["avg_pct_off"] == 25.0  # product 4: best of 20 and 11.1 -> 20; product 5: 30
    assert got["discounting|"]["promotions"] == ["20% off rings"]


def test_template_headlines():
    g = {m["key"]: m for m in moves.group([e(1, "new_product", 1, after={"price": 5}), e(2, "new_product", 2, after={"price": 6}),
                                           e(3, "out_of_stock", 3, category=None), e(4, "new_category", None, category="Men's Rings"),
                                           e(5, "promo_started", None, category=None, after={"text": "Free engraving"})])}
    assert moves.template_headline(g["launched_products|Rings"]) == "Launched 2 products in Rings"
    assert moves.template_headline(g["stock_out|"]) == "Ran out of 1 product"
    assert moves.template_headline(g["new_category|Men's Rings"]) == "Opened a new category: Men's Rings"
    assert moves.template_headline(g["discounting|"]) == "Started a promotion: “Free engraving”"


def test_numbers_guardrail():
    m = {"size": {"count": 14, "avg_pct_off": 22.4, "min_price": 750.0}, "category": "14K Gold Rings",
         "examples": [{"title": "1.5 ct Oval Ring"}]}
    assert moves.numbers_ok("Cut prices on 14 rings, about 22% off", m)
    assert moves.numbers_ok("Launched 14 14K gold rings from $750", m)
    assert moves.numbers_ok("Added a 1.5 ct oval ring", m)
    assert not moves.numbers_ok("Cut prices on 15 rings", m)
    assert not moves.numbers_ok("Up to 40% off", m)
    assert moves.numbers_ok("Started a big promotion", m)
