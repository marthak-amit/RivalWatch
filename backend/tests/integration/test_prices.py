"""Our prices in Magento: request with a preview, apply (Magento first), guardrails, failures, revert, agency approval."""
import json

from tests.integration.test_store_sync import CATALOG, drain, magento
from tests.integration.test_ui_api import signup

TOKEN = "integration-token"


def magento_prices(site, prices: dict, costs: dict, *, refuse: str | None = None):
    """Stand-in for Magento's price REST APIs. `prices` is updated by writes, like the real store."""
    def handle(path, headers):
        if not path.startswith("/rest/V1/products/"):
            return None
        if headers.get("Authorization") != f"Bearer {TOKEN}":
            return 401, {"content-type": "application/json"}, json.dumps({
                "message": "The consumer isn't authorized to access %resources.",
                "parameters": {"resources": "Magento_Catalog::products"}})
        body, kind = json.loads(site.posts[-1][1]), path.rsplit("/", 1)[1]
        if kind == "base-prices-information":
            out = [{"sku": s, "price": prices[s], "store_id": 0} for s in body["skus"] if s in prices]
        elif kind == "cost-information":
            out = [{"sku": s, "cost": costs[s], "store_id": 0} for s in body["skus"] if s in costs]
        elif refuse:
            out = [{"message": "Invalid attribute %fieldName = %fieldValue.", "parameters": ["price", refuse]}]
        else:
            for p in body["prices"]:
                prices[p["sku"]] = p["price"]
            out = []
        return 200, {"content-type": "application/json"}, json.dumps(out)
    site.dynamic.append(handle)


def connect(client, site, conn, token=TOKEN):
    magento(site, CATALOG)
    signup(client)
    assert client.put("/api/store", json={"url": site.base, "token": token}).status_code == 200
    drain(conn)
    return client.get("/api/store").json()["store"]["id"]


def our(client, sku):
    return next(p for p in client.get("/api/products", params={"competitor": "ours"}).json()["products"] if p["sku"] == sku)


def history(conn, sid):
    return conn.execute(f"select type, user_id, price_before, price_after from site_{sid}.history where run_id is null "
                        "order by id").fetchall()


def test_request_preview_apply_and_revert(client, site, conn):
    prices, costs = {"DR-1": 750, "DR-2": 1500}, {"DR-1": 400}
    magento_prices(site, prices, costs)
    sid = connect(client, site, conn)
    me = client.get("/api/me").json()

    r = client.post("/api/prices", json={"sku": "DR-1", "newPrice": 720})
    assert r.status_code == 200, r.text
    change, preview = r.json()["change"], r.json()["preview"]
    assert (change["status"], change["oldPrice"], change["newPrice"], change["changePct"]) == ("pending", 750, 720, -4.0)
    assert (preview["priceSource"], preview["cost"], preview["marginBefore"], preview["marginAfter"]) == ("magento", 400, 46.7, 44.4)
    assert preview["canApply"] is True and preview["position"]["type"] == "engagement ring"
    assert prices["DR-1"] == 750  # nothing written yet

    r = client.post(f"/api/prices/{change['id']}/apply", json={})
    assert r.status_code == 200, r.text
    applied = r.json()["change"]
    assert (applied["status"], applied["appliedBy"], applied["error"]) == ("applied", "ui@test.local", None)
    assert prices["DR-1"] == 720 and our(client, "DR-1")["price"] == 720
    assert [(h["type"], h["price_before"], h["price_after"]) for h in history(conn, sid)] == [("price_drop", 750, 720)]
    assert history(conn, sid)[0]["user_id"] == int(me["id"])
    assert client.post(f"/api/prices/{change['id']}/apply", json={}).status_code == 409  # never twice

    r = client.post(f"/api/prices/{change['id']}/revert", json={})
    assert r.status_code == 200, r.text
    back = r.json()["change"]
    assert (back["status"], back["reverts"], back["newPrice"]) == ("applied", change["id"], 750)
    assert prices["DR-1"] == 750 and our(client, "DR-1")["price"] == 750
    assert [c["status"] for c in client.get("/api/prices").json()["changes"]] == ["applied", "applied"]

    # on sale (1200, was 1500): the regular price moves, the sale price shoppers pay stays
    r = client.post("/api/prices", json={"sku": "DR-2", "newPrice": 1400})
    assert r.json()["preview"]["shownAfter"] == 1200 and "sale price" in r.json()["preview"]["warnings"][0]
    client.post(f"/api/prices/{r.json()['change']['id']}/apply", json={})
    assert prices["DR-2"] == 1400 and (our(client, "DR-2")["price"], our(client, "DR-2")["wasPrice"]) == (1200, "$1,400.00")
    assert history(conn, sid)[-1]["type"] == "product_updated"


def test_guardrails(client, site, conn):
    magento_prices(site, {"DR-1": 750}, {"DR-1": 400})
    connect(client, site, conn)
    errors = {
        300: "more than the 50% one change may move",  # a typo-sized change
        750: "already 750",
        410: "below the 10% minimum",
    }
    assert client.patch("/api/prices/settings", json={"minMarginPct": 10}).json()["minMarginPct"] == 10
    for price, msg in errors.items():
        r = client.post("/api/prices", json={"sku": "DR-1", "newPrice": price})
        assert r.status_code == 400 and msg in r.json()["error"], (price, r.text)
    assert client.post("/api/prices", json={"sku": "NOPE", "newPrice": 10}).status_code == 404
    for bad in (0, -5, 10.555, "abc"):
        assert client.post("/api/prices", json={"sku": "DR-1", "newPrice": bad}).status_code == 400
    assert client.get("/api/prices").json()["changes"] == []  # nothing was created
    # a second request for the same SKU replaces the pending one
    a = client.post("/api/prices", json={"sku": "DR-1", "newPrice": 700}).json()["change"]["id"]
    client.post("/api/prices", json={"sku": "DR-1", "newPrice": 690})
    assert [c["status"] for c in client.get("/api/prices").json()["changes"]] == ["pending", "cancelled"]
    assert client.post(f"/api/prices/{a}/apply", json={}).status_code == 409


def test_magento_refusing_changes_nothing(client, site, conn):
    prices = {"DR-1": 750}
    magento_prices(site, prices, {})
    sid = connect(client, site, conn, token="token-without-catalog-access")
    r = client.post("/api/prices", json={"sku": "DR-1", "newPrice": 720})
    preview = r.json()["preview"]
    assert preview["priceSource"] == "catalog" and "Magento_Catalog::products" in preview["warnings"][0]
    r = client.post(f"/api/prices/{r.json()['change']['id']}/apply", json={})
    assert r.status_code == 502 and "Magento_Catalog::products" in r.json()["error"]
    failed = client.get("/api/prices").json()["changes"][0]
    assert failed["status"] == "failed" and "Magento_Catalog::products" in failed["error"]
    assert prices["DR-1"] == 750 and our(client, "DR-1")["price"] == 750 and history(conn, sid) == []

    # with the right token, a write Magento rejects is also recorded and changes nothing
    client.patch("/api/store", json={"token": TOKEN})
    site.dynamic.pop(0)  # the price handler that accepts writes
    magento_prices(site, prices, {}, refuse="720")
    cid = client.post("/api/prices", json={"sku": "DR-1", "newPrice": 720}).json()["change"]["id"]
    r = client.post(f"/api/prices/{cid}/apply", json={})
    assert r.status_code == 502 and "Invalid attribute price = 720" in r.json()["error"]
    assert prices["DR-1"] == 750 and history(conn, sid) == []


def test_only_the_agency_applies_when_set(client, site, conn, make_admin):
    prices = {"DR-1": 750}
    magento_prices(site, prices, {})
    connect(client, site, conn)
    uid = client.get("/api/me").json()["id"]
    cid = client.post("/api/prices", json={"sku": "DR-1", "newPrice": 700}).json()["change"]["id"]
    client.post("/api/auth/logout", json={})
    email, pw = make_admin()
    client.post("/api/auth/login", json={"email": email, "password": pw})
    assert client.patch(f"/api/admin/users/{uid}/prices/settings", json={"adminOnly": True}).json()["adminOnly"] is True
    client.post("/api/auth/logout", json={})
    client.post("/api/auth/login", json={"email": "ui@test.local", "password": "password123"})

    r = client.post(f"/api/prices/{cid}/apply", json={})
    assert r.status_code == 403 and "agency" in r.json()["error"] and prices["DR-1"] == 750
    assert client.patch("/api/prices/settings", json={"maxChangePct": 90}).status_code == 403
    assert client.post("/api/prices", json={"sku": "DR-1", "newPrice": 710}).json()["preview"]["canApply"] is False

    client.post("/api/auth/logout", json={})
    client.post("/api/auth/login", json={"email": email, "password": pw})
    waiting = client.get("/api/admin/prices", params={"status": "pending"}).json()["changes"]
    mine = [c for c in waiting if c["client"] == "ui@test.local"]
    assert len(mine) == 1 and mine[0]["newPrice"] == 710
    r = client.post(f"/api/admin/prices/{mine[0]['id']}/apply", json={})
    assert r.status_code == 200, r.text
    assert (r.json()["change"]["appliedBy"], r.json()["change"]["requestedBy"]) == (email, "ui@test.local")
    assert prices["DR-1"] == 710
    assert "price_apply" in [a["action"] for a in client.get("/api/admin/audit").json()["audit"]]
