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


def test_uk_karat_plating_and_cubic_zirconia():
    a = attributes.parse("Tennis Everyday Bracelet | 18ct Gold Plated/Cubic Zirconia")
    assert (a["carat"], a["metal"], a["gem"]) == (None, "18k gold plated", "cubic zirconia")
    assert attributes.parse("Tennis Everyday Bracelet | Platinum Plated/Cubic Zirconia")["metal"] == "platinum plated"
    a = attributes.parse("9ct Yellow Gold 0.25ct Diamond Solitaire Ring")
    assert (a["metal"], a["carat"], a["gem"]) == ("9k yellow gold", 0.25, "diamond")
    assert attributes.parse("Molten Gold Vermeil Hoops")["metal"] == "gold vermeil"
    assert attributes.parse("Silver-plated CZ studs")["metal"] == "silver plated"


def test_company_profile_from_the_homepage():
    html = ('<html><head><title>Lumen | Fine Jewellery</title><meta name="description" content="Lab-grown diamonds, made in London.">'
            '</head><body><h1>Brilliance, responsibly made</h1><h2>Free resizing</h2><h3>Lifetime warranty</h3>'
            '<a href="https://www.instagram.com/lumenjewels">ig</a><a href="https://x.com/lumen">x</a><a href="https://x.com/other">x2</a>'
            '<a href="mailto:hello@lumen.example?subject=hi">mail</a><p>Write to care@lumen.example</p></body></html>')
    p = extract.profile(html, "https://lumen.example/", [{"name": "Rings", "url": "https://lumen.example/c/rings"}])
    assert p["headline"] == "Brilliance, responsibly made" and p["description"] == "Lab-grown diamonds, made in London."
    assert p["positioning"] == ["Free resizing", "Lifetime warranty"]
    assert p["socials"] == [{"network": "Instagram", "url": "https://www.instagram.com/lumenjewels"},
                            {"network": "X", "url": "https://x.com/lumen"}]
    assert p["emails"] == ["hello@lumen.example", "care@lumen.example"]
    assert p["keyPages"] == [{"path": "/c/rings", "title": "Rings"}]
    assert html_to_markdown(html, "https://lumen.example/")["description"] == "Lab-grown diamonds, made in London."


MISSOMA_META = """<html><head><title>Molten Teardrop Pavé Charm Hoop Earrings | Missoma UK</title>
<meta name="description" content="Discover the Molten Teardrop Pavé Charm Hoop Earrings in silver from Missoma.">
<meta property="og:site_name" content="Missoma">
<meta property="og:url" content="https://www.missoma.com/products/molten-teardrop-pave-charm-hoop-earrings-silver-plated-cubic-zirconia">
<meta property="og:title" content="Molten Teardrop Pavé Charm Hoop Earrings | Missoma UK">
<meta property="og:type" content="product">
<meta property="og:description" content="Discover the Molten Teardrop Pavé Charm Hoop Earrings in silver from Missoma. Covered by a two-year warranty. Get 10% off your first order."><meta property="og:image" content="http://www.missoma.com/cdn/shop/files/a.webp?v=1790883216">
  <meta property="og:image:secure_url" content="https://www.missoma.com/cdn/shop/files/a.webp?v=1790883216">
  <meta property="og:image:width" content="2351">
  <meta property="og:image:height" content="2953"><meta property="og:price:amount" content="89.00">
  <meta property="og:price:currency" content="GBP"><meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="Molten Teardrop Pavé Charm Hoop Earrings | Missoma UK">
<meta name="twitter:description" content="Discover the Molten Teardrop Pavé Charm Hoop Earrings in silver from Missoma. Covered by a two-year warranty. Get 10% off your first order.">
<meta name="viewport" content="width=device-width"></head><body></body></html>"""


def test_page_meta_tags_are_kept_as_they_are():
    t = extract.page_tags(MISSOMA_META)
    assert t["meta_title"] == "Molten Teardrop Pavé Charm Hoop Earrings | Missoma UK"
    assert t["meta_description"] == "Discover the Molten Teardrop Pavé Charm Hoop Earrings in silver from Missoma."
    m = t["meta"]
    assert m["og:site_name"] == "Missoma" and m["og:type"] == "product" and m["og:price:amount"] == "89.00"
    assert m["og:price:currency"] == "GBP" and m["og:image:width"] == "2351" and m["twitter:card"] == "summary_large_image"
    assert m["og:url"].endswith("silver-plated-cubic-zirconia") and m["og:image"].startswith("http://www.missoma.com/cdn/")
    assert "viewport" not in m  # only SEO/social tags are kept
    [p] = extract.extract_products(MISSOMA_META, "https://www.missoma.com/products/x")  # OpenGraph product
    assert p["meta_title"] == t["meta_title"] and p["meta"]["og:title"] == m["og:title"] and p["meta_fetched_at"]


FIX = __import__("pathlib").Path(__file__).resolve().parent.parent / "fixtures"


def test_server_rendered_page_without_structured_data_is_read_from_its_html():
    html = (FIX / "aura_product.html").read_text()
    assert extract.products_from_jsonld(html, "u") == [] and extract.product_from_og(html, "u") is None
    [p] = extract.extract_products(html, "https://techtitans.bytestechnolabs.com/aura_jewels/product/bangle-wristband-20-1928")
    assert (p["title"], p["price"], p["currency"], p["sku"], p["extracted_by"]) == (
        "Bangle Wristband 20", Decimal("2223.16"), "INR", "BRC-0020", "html")
    assert not extract.needs_browser(html)


def test_client_side_templates_are_never_stored_and_mark_the_page_for_a_browser():
    html = (FIX / "indriya_template.html").read_text()
    assert extract.extract_products(html, "https://www.indriya.com/jewellery-products/x") == []
    assert extract.needs_browser(html)


def test_html_fallback_refuses_ambiguous_pages():
    assert extract.product_from_html("<html><body><h1>Our story</h1><p>Since 1990.</p></body></html>", "u") is None
    assert extract.product_from_html("<html><body><h1>Rings</h1><p>Ring A ₹100</p><p>Ring B ₹200</p></body></html>", "u") is None
    two = "<html><body><h1>Halo Ring</h1><p>₹1,500.00</p><s>₹1,800.00</s></body></html>"
    p = extract.product_from_html(two, "u")
    assert p["price"] == Decimal("1500.00") and p["compare_at_price"] is None  # never guess a sale from page text


def test_category_comes_from_the_breadcrumb_when_the_product_has_none():
    [p] = extract.extract_products((FIX / "aura_product.html").read_text(), "https://x/aura_jewels/product/bangle-wristband-20-1928")
    assert p["category"] == "Bracelet"
    ld = ('<script type="application/ld+json">{"@type":"Product","name":"Lakshmi Necklace","offers":{"price":"5"}}</script>'
          '<script type="application/ld+json">{"@type":"BreadcrumbList","itemListElement":['
          '{"position":1,"name":"Home"},{"position":2,"item":{"name":"Jewellery"}},{"position":3,"name":"Necklaces"},'
          '{"position":4,"name":"Lakshmi Necklace"}]}</script>')
    [p] = extract.extract_products(f"<html><head>{ld}</head></html>", "https://x/p")
    assert p["category"] == "Necklaces"


def test_a_menu_link_back_to_the_homepage_is_not_a_category():
    html = ('<html><body><nav><a href="https://s.com/shop/">✨ Brand</a><a href="/shop/category/ring">Ring</a></nav></body></html>')
    assert [n["name"] for n in extract.page_meta(html, "https://s.com/shop/")["nav"]] == ["Ring"]
