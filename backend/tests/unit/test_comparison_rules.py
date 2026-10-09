from competitor_moves.services import comparison as cmp


def t(title, category=None):
    return cmp.product_type(title, category)


def test_product_type_prefers_the_category_then_the_title():
    assert t("3.06ct D VVS2 Pear-Cut IGI Lab-Grown Diamond Engagement Ring + Matching Band", "Engagement Rings") == "engagement ring"
    assert t("Platinum Engagement and Wedding Band Set", "Engagement Rings") == "engagement ring"  # the shop's category wins
    assert t("Platinum Engagement and Wedding Band Set", "Jewelry") == "set"  # generic category: read the title
    assert t("Tennis Everyday Bracelet | 18ct Gold Plated", "Bracelets") == "bracelet"
    assert t("The Ultimate Triple Ring Set | Silver Plated", "Ring Sets") == "set"
    assert t("Molten Teardrop Pavé Charm Hoop Earrings", "Earrings") == "earrings"
    assert t("Lab Grown Diamond Studs", "Jewelry") == "earrings"
    assert t("Lakshmi Padma Coin Gold Necklace", "Necklaces") == "necklace"
    assert t("Tanirika Gold Pendant", None) == "pendant"
    assert t("Mahashri Padma Gold Bangle", "Bangles") == "bangle"
    assert t("Rolex Datejust 36", "Watches") == "watch"
    assert t("1.01ct Round IGI Certified Diamond", "Loose Diamonds") == "loose diamond"
    assert t("Classic Wedding Band 4mm", "Wedding Bands") == "wedding band"
    assert t("Gift Card", "Jewelry") is None


def test_bands_for_metal_stone_and_carat():
    assert cmp.metal_family("18k gold plated") == "gold plated" and cmp.metal_family("14k white gold") == "gold"
    assert cmp.metal_family("platinum plated") == "plated" and cmp.metal_family("sterling silver") == "silver"
    assert cmp.metal_family("platinum") == "platinum" and cmp.metal_family(None) is None
    assert cmp.stone_band({"stone": "lab_grown", "gem": "diamond"}) == "lab-grown diamond"
    assert cmp.stone_band({"stone": None, "gem": "cubic zirconia"}) == "cubic zirconia"
    assert cmp.stone_band({"stone": "natural", "gem": "diamond"}) == "natural diamond"
    assert cmp.stone_band({"stone": None, "gem": "sapphire"}) == "gemstone" and cmp.stone_band({}) is None
    assert [cmp.carat_band(c) for c in (None, 0.3, 0.5, 1.0, 1.99, 3.06)] == [None, "<0.5ct", "0.5-1ct", "1-2ct", "1-2ct", "2ct+"]


def test_median_and_gap():
    assert cmp.median([5, 1, 3]) == 3 and cmp.median([1, 2, 3, 4]) == 2.5
    assert cmp.gap_pct(80, 100) == -20.0 and cmp.gap_pct(150, 100) == 50.0
