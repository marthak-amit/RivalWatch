"""Our store against competitors, from the database (no crawling needed)."""
import json

from psycopg import sql
from tests.integration.test_ui_api import signup

from competitor_moves.db.site_schema import table
from competitor_moves.services.change_detection import norm_sku


def add_products(conn, site_id, rows):
    for i, (title, price, cur, cat, attrs, sku) in enumerate(rows):
        conn.execute(sql.SQL("insert into {} (url, title, price, currency, category, attributes, sku_norm) "
                             "values (%s,%s,%s,%s,%s,%s,%s)").format(table(site_id, "products")),
                     (f"https://x/{site_id}/{i}", title, price, cur, cat, json.dumps(attrs), norm_sku(sku)))


def workspace(client, conn, make_site):
    signup(client)
    uid = conn.execute("select id from users where email='ui@test.local'").fetchone()["id"]
    pid = conn.execute("select id from projects where owner_user_id=%s", (uid,)).fetchone()["id"]

    def site(role, name, rows):
        sid = make_site(f"https://{name}.example/", role=role, name=name)
        conn.execute("update sites set project_id=%s where id=%s", (pid, sid))
        add_products(conn, sid, rows)
        return sid
    lab = {"stone": "lab_grown", "gem": "diamond", "metal": "14k white gold"}
    ours = site("ours", "Louped", [("Lab Ring A", 1000, "USD", "Engagement Rings", lab, "ab1"), ("Lab Ring B", 1200, "USD", "Engagement Rings", lab, None),
                                   ("Lab Ring C", 1400, "USD", "Engagement Rings", lab, None), ("Test Product", 1, "USD", "Engagement Rings", {}, None),
                                   ("Gold Hoop Earrings", 300, "USD", "Jewelry", {"metal": "14k gold"}, None)])
    us = site("competitor", "BrightUS", [("Ring X", 900, "USD", "Engagement Rings", lab, "AB-1"), ("Ring Y", 950, "USD", "Engagement Rings", lab, None),
                                          ("Ring Z", 1000, "USD", "Engagement Rings", lab, None),
                                          ("Pearl Necklace 1", 400, "USD", "Necklaces", {}, None), ("Pearl Necklace 2", 500, "USD", "Necklaces", {}, None),
                                          ("Pearl Necklace 3", 600, "USD", "Necklaces", {}, None)])
    uk = site("competitor", "LondonCo", [(f"Ring {i}", 800 + i, "GBP", "Engagement Rings", lab, None) for i in range(3)])
    return ours, us, uk


def test_prices_compared_by_type_with_gaps_matches_and_gaps_in_range(client, conn, make_site):
    _ours, us, _uk = workspace(client, conn, make_site)
    r = client.get("/api/comparison").json()
    assert r["ours"]["currency"] == "USD" and r["ours"]["products"] == 4  # the $1 test product is below the price floor
    [rings] = [row for row in r["rows"] if row["key"]["type"] == "engagement ring"]
    assert rings["ours"] == {"currency": "USD", "count": 3, "median": 1200.0, "min": 1000.0, "max": 1400.0}
    by = {c["name"]: c for c in rings["competitors"]}
    assert by["BrightUS"]["median"] == 950.0 and by["BrightUS"]["gapPct"] == -20.8 and rings["cheapest"] == str(us)
    assert by["LondonCo"]["comparable"] is False and "gapPct" not in by["LondonCo"]
    assert any("GBP" in n for n in r["notes"])
    assert [g["label"] for g in r["onlyCompetitors"]] == ["necklace"]  # they sell necklaces, we don't
    [m] = r["matches"]  # same SKU after normalisation (AB-1 vs ab1)
    assert (m["ours"]["price"], m["theirs"]["price"], m["gapPct"]) == (1000.0, 900.0, -10.0)
    by_stone = client.get("/api/comparison", params={"group_by": "type,stone"}).json()
    assert by_stone["rows"][0]["label"] == "engagement ring · lab-grown diamond"


def test_settings_change_the_comparison(client, conn, make_site):
    workspace(client, conn, make_site)
    assert client.get("/api/comparison/settings").json() == {"min_price": 10.0, "min_group_size": 3, "fx_rates": {}}
    s = client.patch("/api/comparison/settings", json={"fxRates": {"GBP": 1.25}, "minPrice": 0}).json()
    assert s["fx_rates"] == {"GBP": 1.25} and s["min_price"] == 0
    r = client.get("/api/comparison").json()
    rings = next(row for row in r["rows"] if row["key"]["type"] == "engagement ring")
    uk = next(c for c in rings["competitors"] if c["name"] == "LondonCo")
    assert uk["comparable"] and uk["medianInOurCurrency"] == 1001.25 and r["ours"]["products"] == 5
    for bad in ({"fxRates": {"pounds": 1.2}}, {"fxRates": {"GBP": -1}}, {"minGroupSize": 0}, {"minPrice": -5}):
        assert client.patch("/api/comparison/settings", json=bad).status_code == 400, bad
    assert client.get("/api/comparison", params={"group_by": "brand"}).status_code == 400


def test_without_our_store_the_comparison_says_how_to_start(client):
    signup(client)
    r = client.get("/api/comparison").json()
    assert r["ours"] is None and "Connect your store" in r["notes"][0]
