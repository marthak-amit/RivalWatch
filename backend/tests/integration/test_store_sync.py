"""Our own Magento store: connect, sync the catalog through GraphQL, see our own changes in history."""
import json
from urllib.parse import parse_qs, urlsplit

from tests.integration.test_ui_api import signup

from competitor_moves.workers import worker


def magento(site, catalog: list[dict]):
    """A stand-in Magento GraphQL endpoint serving `catalog` 2 products per page. Like Magento 2.4.4+, it fails any query
    that carries an integration token as a Bearer, so these tests also check the token never goes to GraphQL."""
    def handle(path, headers):
        p = urlsplit(path)
        if p.path != "/graphql":
            return None
        if headers.get("Authorization"):
            return 200, {"content-type": "application/json"}, json.dumps({"errors": [{"message": "Composite reader could not read a token"}]})
        q = parse_qs(p.query)
        if "storeConfig" in q["query"][0]:
            data = {"storeConfig": {"store_name": "Louped", "base_currency_code": "USD", "base_url": site.base + "/",
                                    "product_url_suffix": ".html"}}
        else:
            page = json.loads(q["variables"][0])["page"]
            items = catalog[(page - 1) * 2: page * 2]
            data = {"products": {"total_count": len(catalog), "page_info": {"total_pages": (len(catalog) + 1) // 2, "current_page": page},
                                 "items": items}}
        return 200, {"content-type": "application/json"}, json.dumps({"data": data})
    site.dynamic.append(handle)


def item(sku, name, price, *, regular=None, stock="IN_STOCK", created="2025-01-01 10:00:00", cat="Engagement Rings"):
    return {"sku": sku, "name": name, "url_key": sku.lower(), "url_suffix": ".html", "created_at": created,
            "updated_at": created, "stock_status": stock, "meta_title": name, "meta_description": f"Buy {name}",
            "categories": [{"name": "Jewellery", "level": 1}, {"name": cat, "level": 2}], "small_image": {"url": "https://img/x.jpg"},
            "price_range": {"minimum_price": {"regular_price": {"value": regular or price, "currency": "USD"},
                                              "final_price": {"value": price, "currency": "USD"}}}}


CATALOG = [item("DR-1", "1.00 ct Marquise Engagement Ring 14K White Gold", 750),
           item("DR-2", "Platinum Wedding Band", 1200, regular=1500),
           item("DR-3", "Lab-Grown Diamond Studs", 900, cat="Earrings")]


def drain(conn):
    while worker.run_once(conn):
        pass


def test_connect_sync_and_see_our_own_changes(client, site, conn):
    magento(site, CATALOG)
    signup(client)
    r = client.put("/api/store", json={"url": site.base, "storeCode": "default"})
    assert r.status_code == 200, r.text
    store = r.json()["store"]
    assert (store["name"], store["currency"], store["platform"], store["hasToken"], store["syncing"]) == ("Louped", "USD", "magento", False, True)
    drain(conn)
    store = client.get("/api/store").json()["store"]
    assert store["productCount"] == 3 and store["lastSummary"] == f"Synced 3 of 3 products from {site.base.removeprefix('http://')}; 1 changes"
    ours = client.get("/api/products", params={"competitor": "ours", "sort": "price_asc"}).json()
    assert [p["title"] for p in ours["products"]] == ["1.00 ct Marquise Engagement Ring 14K White Gold", "Lab-Grown Diamond Studs",
                                                       "Platinum Wedding Band"]
    ring = ours["products"][0]
    assert (ring["priceLabel"], ring["category"], ring["attributes"]["metal"], ring["metaTitle"]) == (
        "$750.00", "Engagement Rings", "14k white gold", "1.00 ct Marquise Engagement Ring 14K White Gold")
    assert ours["products"][2]["wasPrice"] == "$1,500.00"  # our own sale price is read too
    sid = store["id"]
    # we change a price and remove a product in Magento; the next sync logs both as our store's history
    magento_changed = [item("DR-1", "1.00 ct Marquise Engagement Ring 14K White Gold", 699), CATALOG[1]]
    site.dynamic.clear()
    magento(site, magento_changed)
    for _ in range(2):  # a product counts as removed after two full reads without it
        assert client.post("/api/store/sync", json={}).json()["queued"] is True
        drain(conn)
    types = [r["type"] for r in conn.execute(f"select type from site_{sid}.history order by id").fetchall()]
    assert types == ["on_sale", "price_drop", "product_removed"]
    assert client.get("/api/state").json()["store"]["productCount"] == 2
    assert client.get("/api/state").json()["competitors"] == []  # our store is not a competitor


def test_the_token_is_encrypted_never_returned_and_errors_are_clear(client, site, conn):
    magento(site, CATALOG)
    signup(client)
    r = client.put("/api/store", json={"url": site.base + "/nothing-here"})
    assert r.status_code == 400 and "Couldn't read the store's catalog API" in r.json()["error"]
    r = client.put("/api/store", json={"url": site.base, "token": "tok-123"})
    assert r.status_code == 200 and r.json()["store"]["hasToken"] is True and "tok-123" not in r.text
    stored = conn.execute("select token_encrypted from magento_connections mc join projects p on p.id = mc.project_id "
                          "join users u on u.id = p.owner_user_id where u.email='ui@test.local'").fetchone()["token_encrypted"]
    assert stored and "tok-123" not in stored
    drain(conn)
    assert client.get("/api/store").json()["store"]["productCount"] == 3
    for bad in ("http://10.0.0.1/", "not a url"):
        assert client.put("/api/store", json={"url": bad}).status_code == 400
    assert client.request("DELETE", "/api/store", json={}).json() == {"ok": True}
    assert client.get("/api/store").json() == {"store": None}


def test_store_settings_can_change_without_reconnecting(client, site, conn):
    magento(site, CATALOG)
    signup(client)
    assert client.patch("/api/store", json={"cron": "0 5 * * *"}).status_code == 404  # nothing connected yet
    client.put("/api/store", json={"url": site.base, "token": "tok-2"})
    r = client.patch("/api/store", json={"cron": "30 4 * * *", "enabled": False})
    assert r.status_code == 200 and (r.json()["store"]["cron"], r.json()["store"]["enabled"]) == ("30 4 * * *", False)
    assert client.patch("/api/store", json={"token": ""}).json()["store"]["hasToken"] is False
    assert client.patch("/api/store", json={"token": "tok-3"}).json()["store"]["hasToken"] is True
    r = client.patch("/api/store", json={"storeCode": "default"})
    assert r.status_code == 200 and r.json()["store"]["hasToken"] is True  # left out: the token is kept
    assert client.patch("/api/store", json={"cron": "every hour"}).status_code == 400
    r = client.patch("/api/store", json={"enabled": True, "storeCode": "default"})
    assert r.json()["store"]["enabled"] is True and r.json()["store"]["syncing"] is True


def test_admin_sets_up_a_clients_store(client, site, conn, make_admin):
    magento(site, CATALOG)
    signup(client, "client@test.local")
    client.post("/api/auth/logout", json={})
    email, pw = make_admin()
    client.post("/api/auth/login", json={"email": email, "password": pw})
    uid = next(u["id"] for u in client.get("/api/admin/users").json()["users"] if u["email"] == "client@test.local")
    assert client.get(f"/api/admin/users/{uid}/store").json() == {"store": None}
    r = client.put(f"/api/admin/users/{uid}/store", json={"url": site.base, "cron": "0 3 * * *"})
    assert r.status_code == 200 and r.json()["store"]["name"] == "Louped" and r.json()["store"]["cron"] == "0 3 * * *"
    assert client.patch(f"/api/admin/users/{uid}/store", json={"enabled": False}).json()["store"]["enabled"] is False
    actions = [a["action"] for a in client.get("/api/admin/audit").json()["audit"]]
    assert {"store_connect", "store_update"} <= set(actions)
    admin_id = next(u["id"] for u in client.get("/api/admin/users").json()["users"] if u["email"] == email)
    assert client.get(f"/api/admin/users/{admin_id}/store").status_code == 400
    client.post("/api/auth/logout", json={})
    client.post("/api/auth/login", json={"email": "client@test.local", "password": "password123"})
    mine = client.get("/api/store").json()["store"]  # the client sees the store the admin set up
    assert mine["url"] == site.base and mine["enabled"] is False
    assert client.get(f"/api/admin/users/{uid}/store").status_code == 403
    assert client.request("DELETE", f"/api/admin/users/{uid}/store", json={}).status_code == 403


def test_a_missing_encryption_key_is_a_clear_503_not_a_bare_500(client, site, monkeypatch):
    from competitor_moves.core import crypto
    magento(site, CATALOG)
    signup(client)
    monkeypatch.setattr(crypto, "get_settings", lambda: type("S", (), {"app_encryption_key": ""})())
    r = client.put("/api/store", json={"url": site.base, "token": "secret"})
    assert r.status_code == 503 and "APP_ENCRYPTION_KEY" in r.json()["error"], r.text
    assert client.put("/api/store", json={"url": site.base}).status_code == 200  # no token to store: still fine
