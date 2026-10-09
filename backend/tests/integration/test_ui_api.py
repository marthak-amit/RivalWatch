"""The RivalWatch UI's /api/* contract, driven the way the pages call it (cookie session, JSON bodies)."""

from tests.agents.test_crawl_graph import CATALOG, NEW_COLLECTION, shopify

from competitor_moves.workers import worker


def signup(client, email="ui@test.local", plan="starter"):
    r = client.post("/api/auth/signup", json={"email": email, "name": "Dana", "password": "password123", "plan": plan})
    assert r.status_code == 200, r.text
    return r.json()


def drain(conn):
    while worker.run_once(conn):
        pass


def test_signup_session_me_logout(client):
    body = signup(client)
    assert body["redirect"] == "/app" and body["user"]["name"] == "Dana" and body["user"]["status"] == "active"
    assert client.cookies.get("rw_session")
    me = client.get("/api/me").json()
    assert me["apiKey"].startswith("rw_") and me["planInfo"]["competitors"] == 3 and me["credits"] == 300
    assert client.get("/api/session").json()["user"]["email"] == "ui@test.local"
    assert client.post("/api/me/plan", json={"plan": "pro"}).json()["credits"] == 2000
    old = me["apiKey"]
    assert client.post("/api/me/rotate-key", json={}).json()["apiKey"] != old
    assert client.post("/api/auth/logout", json={}).json() == {"ok": True}
    assert client.get("/api/session").json() == {"user": None}
    r = client.get("/api/me")
    assert r.status_code == 401 and r.json() == {"error": "Not signed in"}


def test_login_errors_json_only_and_rate_limit(client):
    signup(client)
    client.post("/api/auth/logout", json={})
    r = client.post("/api/auth/login", json={"email": "ui@test.local", "password": "nope-nope"})
    assert r.status_code == 401 and r.json()["error"] == "Incorrect email or password"
    assert client.post("/api/auth/login", content="email=x", headers={"content-type": "application/x-www-form-urlencoded"}).status_code == 415
    assert client.post("/api/auth/signup", json={"email": "bad", "password": "password123"}).json()["error"] == "Enter a valid email address"
    for _ in range(10):
        client.post("/api/auth/login", json={"email": "ui@test.local", "password": "wrong-pass"})
    assert client.post("/api/auth/login", json={"email": "ui@test.local", "password": "password123"}).status_code == 429


def test_pages_redirect_like_the_original_server(client):
    assert client.get("/", follow_redirects=False).status_code == 200
    r = client.get("/app", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/login"
    signup(client)
    r = client.get("/app", follow_redirects=False)
    assert r.status_code == 200 and "default-src 'self'" in r.headers["content-security-policy"]
    assert client.get("/admin", follow_redirects=False).headers["location"] == "/app"


def test_competitor_lifecycle_with_scope_sort_and_dashboard(client, site, conn):
    shopify(site, CATALOG, NEW_COLLECTION)
    signup(client)
    assert client.post("/api/crawl", json={}).json() == {"skipped": True, "changes": 0}
    r = client.post("/api/competitors", json={"name": "Lumen", "url": site.base + "/", "maxProducts": 50, "sort": "price_asc"})
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    s = client.get("/api/state").json()
    [c] = s["competitors"]
    assert (c["id"], c["name"], c["enabled"], c["snapshot"], c["maxProducts"], c["sort"]) == (cid, "Lumen", True, None, 50, "price_asc")
    assert s["running"] is True and s["plan"]["name"] == "Starter" and isinstance(s["nextRun"], int)
    drain(conn)  # the worker runs the queued first crawl
    s = client.get("/api/state").json()
    [c] = s["competitors"]
    snap = c["snapshot"]
    assert snap["productCount"] == 3 and len(snap["products"]) == 3 and snap["promotions"] == ["Free shipping over $500"]
    assert snap["profile"]["headline"] == "Fine jewellery" and c["snapshots"] == 1 and c["since"]
    assert {p["price"] for p in snap["products"].values()} == {"£750.00", "£80.00", "£300.00"}
    types = {x["type"] for x in s["changes"]}
    assert types == {"product_added", "on_sale", "new_category"} and all(x["signal"] in ("product", "page") for x in s["changes"])
    added = next(x for x in s["changes"] if x["type"] == "product_added")
    assert added["label"] == "New product: Oval Halo Ring at £750.00" and added["competitorId"] == cid
    assert s["digest"]["source"] == "rules" and 3 <= len(s["digest"]["actions"]) <= 5 and s["running"] is False
    week = client.get("/api/reports", params={"period": "week", "competitor": cid}).json()
    assert week["total"] == 3 and week["byType"]["product_added"] == 1 and week["byCompetitor"][0]["id"] == cid
    assert week["crawls"]["count"] == 1 and len(week["daily"]) == 7
    assert client.post("/api/reports/summary", json={"period": "week"}).json()["source"] == "rules"

    cats = client.get(f"/api/competitors/{cid}/categories").json()["categories"]
    assert cats == [{"name": "Rings", "url": f"{site.base}/collections/rings"}, {"name": "Earrings", "url": f"{site.base}/collections/earrings"}]
    r = client.patch(f"/api/competitors/{cid}", json={"categories": cats[:1], "sort": "newest", "cron": "30 6 * * *", "maxProducts": 10})
    assert r.status_code == 200 and r.json()["competitor"]["categories"] == cats[:1] and r.json()["competitor"]["cron"] == "30 6 * * *"
    for bad in ({"sort": "ratings"}, {"cron": "every day"}, {"maxProducts": 0}, {"enabled": "yes"},
                {"categories": [{"name": "X", "url": "https://elsewhere.example/c"}]}):
        r = client.patch(f"/api/competitors/{cid}", json=bad)
        assert r.status_code == 400 and r.json()["error"], bad

    assert client.patch(f"/api/competitors/{cid}", json={"enabled": False}).json()["enabled"] is False
    assert client.post("/api/crawl", json={}).json()["skipped"] is True  # paused sites aren't crawled
    assert client.patch(f"/api/competitors/{cid}", json={"enabled": True}).json()["enabled"] is True
    assert conn.execute("select count(*) as n from jobs where status='queued' and payload->>'site_id'=%s", (cid,)).fetchone()["n"] == 1

    assert client.request("DELETE", f"/api/competitors/{cid}", json={}).json() == {"ok": True}
    assert client.get("/api/state").json()["competitors"] == []
    assert conn.execute("select count(*) as n from information_schema.schemata where schema_name=%s", (f"site_{cid}",)).fetchone()["n"] == 0


def test_add_competitor_rules(client, site):
    signup(client)
    for url, msg in [("not a url", "Enter a valid http(s) URL"), ("http://10.0.0.5/", "private/internal addresses are blocked")]:
        r = client.post("/api/competitors", json={"url": url})
        assert r.status_code == 400 and r.json()["error"] == msg
    assert client.post("/api/competitors", json={"url": site.base + "/"}).status_code == 200
    assert client.post("/api/competitors", json={"url": site.base + "/other"}).json()["error"] == "Already monitoring this site"
    r = client.post("/api/competitors", json={"url": "https://example.com/", "sort": "cheapest"})
    assert r.status_code == 400 and "sort" in r.json()["error"]
    for host in ("example.com", "example.org"):
        assert client.post("/api/competitors", json={"url": f"https://{host}/"}).status_code == 200
    r = client.post("/api/competitors", json={"url": "https://example.net/"})
    assert r.status_code == 403 and r.json()["error"] == "Your Starter plan allows 3 competitors. Upgrade to add more."


def test_product_search_filters_and_sort(client, site, conn):
    shopify(site, CATALOG + [{"title": "Platinum Plated Hoops | Cubic Zirconia", "handle": "hoops", "product_type": "Earrings",
                              "published_at": "2026-01-01T00:00:00Z", "variants": [{"price": "45", "available": False}]}])
    signup(client)
    client.post("/api/competitors", json={"url": site.base + "/"})
    drain(conn)
    allp = client.get("/api/products").json()
    assert allp["total"] == 4 and allp["facets"]["categories"] == ["Earrings", "Rings"] and allp["facets"]["currencies"] == ["GBP"]
    assert "cubic zirconia" in allp["facets"]["gems"]

    def titles(**params):
        return [p["title"] for p in client.get("/api/products", params=params).json()["products"]]
    assert titles(sort="price_asc") == ["Platinum Plated Hoops | Cubic Zirconia", "Pave Band", "Stud Earrings", "Oval Halo Ring"]
    assert titles(category="earrings", sort="price_desc") == ["Stud Earrings", "Platinum Plated Hoops | Cubic Zirconia"]
    assert titles(on_sale="true") == ["Pave Band"] and titles(metal="platinum") == ["Platinum Plated Hoops | Cubic Zirconia"]
    assert titles(min_price=100, max_price=400) == ["Stud Earrings"] and titles(in_stock="false") == ["Platinum Plated Hoops | Cubic Zirconia"]
    assert titles(q="ring") == ["Oval Halo Ring"] and titles(sort="discount")[0] == "Pave Band"
    sale = client.get("/api/products", params={"on_sale": "true"}).json()["products"][0]
    assert (sale["priceLabel"], sale["wasPrice"], sale["discountPct"]) == ("£80.00", "£100.00", 20.0)
    assert client.get("/api/products", params={"sort": "ratings"}).status_code == 400


def test_admin_panel(client, make_admin, conn):
    signup(client, "cust@test.local")
    client.post("/api/auth/logout", json={})
    email, pw = make_admin()
    r = client.post("/api/auth/login", json={"email": email, "password": pw})
    assert r.json()["redirect"] == "/admin"
    stats = client.get("/api/admin/stats").json()
    assert stats["byPlan"]["starter"] >= 1 and stats["admins"] >= 1
    rows = {u["email"]: u for u in client.get("/api/admin/users", params={"q": "test.local"}).json()["users"]}
    cust = rows["cust@test.local"]
    assert cust["creditLimit"] == 300 and cust["competitors"] == 0
    assert client.post("/api/admin/users", json={"email": "x@test.local", "password": "password123", "role": "admin"}).status_code == 403
    assert client.patch(f"/api/admin/users/{rows['boss@test.local']['id']}", json={"plan": "pro"}).status_code == 403
    assert client.post("/api/admin/users", json={"email": "new@test.local", "password": "password123", "plan": "pro"}).json()["credits"] == 2000
    assert client.patch(f"/api/admin/users/{cust['id']}", json={"plan": "business"}).json()["credits"] == 10000
    assert client.patch(f"/api/admin/users/{cust['id']}", json={"status": "suspended"}).json()["status"] == "suspended"
    assert client.post(f"/api/admin/users/{cust['id']}/reset-credits", json={}).json()["credits"] == 10000
    actions = [a["action"] for a in client.get("/api/admin/audit").json()["audit"]]
    assert {"create_user", "update_user", "reset_credits"} <= set(actions)
    assert client.request("DELETE", f"/api/admin/users/{cust['id']}", json={}).json() == {"ok": True}
    client.post("/api/auth/logout", json={})
    r = client.post("/api/auth/login", json={"email": "new@test.local", "password": "password123"})
    assert r.json()["redirect"] == "/app" and client.get("/api/admin/stats").status_code == 403


def test_crawl_settings_are_validated_merged_and_reset(client, site):
    signup(client)
    cid = client.post("/api/competitors", json={"url": site.base + "/", "crawlSettings": {"browser": "off", "delay_sec": 1.5}}).json()["id"]
    view = client.get("/api/state").json()["competitors"][0]
    assert view["crawlSettings"]["browser"] == "off" and view["crawlSettings"]["delay_sec"] == 1.5
    assert view["crawlSettings"]["ai_extract"] is True  # server default where the site has no override
    r = client.patch(f"/api/competitors/{cid}", json={"crawlSettings": {"ai_extract_cap": 5}})
    assert {"browser": "off", "delay_sec": 1.5, "ai_extract_cap": 5}.items() <= r.json()["competitor"]["crawlSettings"].items()
    r = client.patch(f"/api/competitors/{cid}", json={"crawlSettings": {"browser": None}})
    assert r.json()["competitor"]["crawlSettings"]["browser"] == "auto"  # null = back to the default
    for bad in ({"browser": "always"}, {"delay_sec": 0}, {"time_limit_sec": 99999}, {"ai_extract": "yes"},
                {"browser_pages": 1.5}, {"speed": "fast"}):
        r = client.patch(f"/api/competitors/{cid}", json={"crawlSettings": bad})
        assert r.status_code == 400 and r.json()["error"], bad
