from decimal import Decimal

from competitor_moves.crawler import attributes, extract, platforms
from competitor_moves.crawler.markdown import html_to_markdown


def page(*jsonld, head="", body=""):
    scripts = "".join(f'<script type="application/ld+json">{j}</script>' for j in jsonld)
    return f"<html><head><title>T</title>{head}{scripts}</head><body>{body}</body></html>"


def test_simple_product_with_offer():
    html = page('{"@context":"https://schema.org","@type":"Product","name":"1.00 ct Oval Lab-Grown Diamond Ring 14K White Gold",'
                '"sku":"R-100","gtin13":"0123456789012","brand":{"@type":"Brand","name":"Lumen"},"image":["https://s/i.jpg"],'
                '"offers":{"@type":"Offer","price":"1,299.00","priceCurrency":"USD","availability":"https://schema.org/InStock"}}')
    [p] = extract.extract_products(html, "https://s/p/r100")
    assert p["url"] == "https://s/p/r100" and p["sku"] == "R-100" and p["gtin"] == "0123456789012"
    assert p["price"] == Decimal("1299.00") and p["currency"] == "USD" and p["in_stock"] is True
    assert p["brand"] == "Lumen" and p["image"] == "https://s/i.jpg" and p["extracted_by"] == "jsonld"
    assert p["attributes"] == {"carat": 1.0, "metal": "14k white gold", "shape": "oval", "stone": "lab_grown", "gem": "diamond"}


def test_graph_aggregate_offer_and_strikethrough():
    html = page('{"@graph":[{"@type":"WebSite","name":"S"},{"@type":["Product"],"name":"Band",'
                '"offers":[{"@type":"Offer","price":"500","priceCurrency":"USD","availability":"OutOfStock",'
                '"priceSpecification":{"@type":"UnitPriceSpecification","priceType":"https://schema.org/StrikethroughPrice","price":"650"}},'
                '{"@type":"Offer","price":"520"}]}]}')
    [p] = extract.extract_products(html, "https://s/band")
    assert p["price"] == Decimal(500) and p["compare_at_price"] == Decimal(650) and p["in_stock"] is False
    html = page('{"@type":"Product","name":"Studs","offers":{"@type":"AggregateOffer","lowPrice":"199.5","highPrice":"900","priceCurrency":"EUR"}}')
    [p] = extract.extract_products(html, "https://s/studs")
    assert p["price"] == Decimal("199.5") and p["currency"] == "EUR" and p["compare_at_price"] is None


def test_product_group_takes_cheapest_variant_and_ignores_variants_as_products():
    html = page('{"@type":"ProductGroup","name":"Solitaire","productGroupID":"G1","hasVariant":['
                '{"@type":"Product","sku":"G1-14K","offers":{"price":"900"}},{"@type":"Product","sku":"G1-PT","offers":{"price":"1400"}}]}')
    [p] = extract.extract_products(html, "https://s/sol")
    assert p["sku"] == "G1-14K" and p["price"] == Decimal(900)


def test_listing_page_with_several_products_uses_their_own_urls():
    html = page('[{"@type":"Product","name":"A","url":"/p/a","offers":{"price":"10"}},'
                '{"@type":"Product","name":"B","url":"https://s/p/b#x","offers":{"price":"20"}},{"@type":"Product","name":"no url"}]')
    assert [(p["url"], p["price"]) for p in extract.extract_products(html, "https://s/c/rings")] == [
        ("https://s/p/a", Decimal(10)), ("https://s/p/b", Decimal(20))]


def test_no_price_is_none_not_zero_and_bad_json_is_skipped():
    [p] = extract.extract_products(page('{bad json', '{"@type":"Product","name":"X"}'), "https://s/x")
    assert p["price"] is None and p["in_stock"] is None


def test_opengraph_fallback_and_empty_page():
    head = ('<meta property="og:title" content="Pearl Necklace"><meta property="product:price:amount" content="249.00">'
            '<meta property="product:price:currency" content="GBP"><meta property="product:availability" content="instock">')
    [p] = extract.extract_products(page(head=head), "https://s/pearl")
    assert (p["price"], p["currency"], p["in_stock"], p["extracted_by"]) == (Decimal("249.00"), "GBP", True, "og")
    assert extract.extract_products(page(body="<p>About us</p>"), "https://s/about") == []


def test_shopify_feed_product():
    p = extract.shopify_product({"title": "Marquise Ring", "handle": "marquise-ring", "vendor": "Lumen", "product_type": "Rings",
                                 "published_at": "2026-10-05T10:00:00-04:00", "images": [{"src": "https://cdn/i.jpg"}],
                                 "variants": [{"sku": "M-2", "price": "800.00", "compare_at_price": "1000.00", "available": False, "barcode": ""},
                                              {"sku": "M-1", "price": "750.00", "compare_at_price": None, "available": True}]},
                                "https://shop.com/")
    assert p["url"] == "https://shop.com/products/marquise-ring" and p["sku"] == "M-1" and p["price"] == Decimal("750.00")
    assert p["compare_at_price"] is None and p["in_stock"] is True and p["source_date"].isoformat() == "2026-10-05T10:00:00-04:00"
    p = extract.shopify_product({"title": "X", "handle": "x", "variants": [{"price": "80", "compare_at_price": "100"}]}, "https://s")
    assert p["compare_at_price"] == Decimal(100) and p["in_stock"] is None


def test_woocommerce_feed_product_uses_minor_units():
    p = extract.woo_product({"name": "Hoop &amp; Drop", "permalink": "https://w.com/p/hoop", "sku": "H1", "is_in_stock": True,
                             "categories": [{"name": "Earrings"}],
                             "prices": {"price": "12900", "regular_price": "15900", "currency_code": "USD", "currency_minor_unit": 2}})
    assert (p["title"], p["price"], p["compare_at_price"], p["category"]) == ("Hoop & Drop", Decimal(129), Decimal(159), "Earrings")


def test_page_meta_menu_and_promotions():
    html = ('<html><head><title>Lumen</title><meta name="description" content="Fine jewellery"></head><body>'
            '<header><nav><a href="/c/rings">Rings</a><a href="https://www.s.com/c/earrings">Earrings</a>'
            '<a href="https://other.com/x">Partner</a><a href="javascript:void(0)">Menu</a><a href="/c/rings">Rings again</a></nav></header>'
            '<h1>Autumn collection</h1><div>Get 20% off lab-grown rings this week</div><p>Free shipping on orders over $500</p>'
            '<p>We love jewellery.</p></body></html>')
    m = extract.page_meta(html, "https://s.com/")
    assert m["title"] == "Lumen" and m["description"] == "Fine jewellery" and m["h1"] == "Autumn collection"
    assert m["nav"] == [{"name": "Rings", "url": "https://s.com/c/rings"}, {"name": "Earrings", "url": "https://www.s.com/c/earrings"}]
    assert m["promotions"] == ["Get 20% off lab-grown rings this week", "Free shipping on orders over $500"]


def test_attributes():
    assert attributes.parse("1 1/2 ct. t.w. Emerald-Cut Moissanite Ring in Platinum") == {
        "carat": 1.5, "metal": "platinum", "shape": "emerald", "stone": "moissanite", "gem": None}
    assert attributes.parse("Emerald and Diamond Halo Pendant, 18K Yellow Gold")["gem"] == "emerald"
    assert attributes.parse("Sterling Silver Heart Locket")["shape"] is None
    assert attributes.parse("3.06ct D VVS2 Pear-Cut IGI Certified Lab-Grown Diamond Engagement Ring")["carat"] == 3.06
    assert attributes.parse("Platinum Engagement and Wedding Band Set")["metal"] == "platinum"
    assert attributes.parse("") == {"carat": None, "metal": None, "shape": None, "stone": None, "gem": None}


def test_platform_detection_and_feeds():
    assert platforms.detect('<script src="https://cdn.shopify.com/s/x.js">') == "shopify"
    assert platforms.detect('<body class="woocommerce-page">') == "woocommerce"
    assert platforms.detect('<script type="text/x-magento-init">') == "magento"
    assert platforms.detect("<html></html>") == "generic"
    assert platforms.feed_url("shopify", "https://s.com/", 2) == "https://s.com/products.json?limit=250&page=2"
    assert platforms.feed_url("magento", "https://s.com", 1) is None


def test_markdown_matches_ui_shape():
    out = html_to_markdown('<html><head><title>A &amp; B</title><style>x{}</style></head><body><nav><a href="/p">Pricing</a></nav>'
                           '<h2>Plans</h2><ul><li><strong>Pro</strong> — $79/mo</li></ul><script>evil()</script></body></html>',
                           "https://s.com/a/")
    assert out["title"] == "A & B"
    assert "## Plans" in out["markdown"] and "- **Pro** — $79/mo" in out["markdown"] and "evil" not in out["markdown"]
    assert out["links"] == ["https://s.com/p"]


def test_cheapest_offer_wins_even_when_listed_later():
    html = page('{"@type":"Product","name":"Hoops","offers":[{"price":"900","priceCurrency":"USD"},{"price":"750","priceCurrency":"USD"}]}')
    assert extract.extract_products(html, "https://s/h")[0]["price"] == Decimal(750)
