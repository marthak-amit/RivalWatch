from competitor_moves.db.pool import get_pool
from competitor_moves.workers import worker


def sql(q, *a):
    with get_pool().connection() as c:
        return c.execute(q, a).fetchall()


def api_key(client, email="api@test.local"):
    tok = client.post("/user/signup", json={"email": email, "password": "password123"}).json()["token"]
    me = client.get("/user/me", headers={"Authorization": f"Bearer {tok}"}).json()
    assert me["plan"] == "starter" and me["credits"] == 300 and me["api_key"].startswith("rw_")
    return {"x-api-key": me["api_key"]}


def set_credits(email, n):
    sql("update users set credits=%s where email=%s returning 1", n, email)


def shop(site):
    site.routes["/robots.txt"] = (200, {"content-type": "text/plain"}, "User-agent: *\nDisallow: /private\n")
    site.routes["/"] = (200, {}, ('<html><head><title>Lumen Jewels</title></head><body><nav><a href="/rings">Rings</a>'
                                   '<a href="/private/x">Staff</a><a href="https://other.example/x">Ext</a></nav>'
                                   '<h1>Fine jewellery</h1><blockquote>20% off lab-grown rings</blockquote></body></html>'))
    site.routes["/rings"] = (200, {}, '<html><body><h2>Rings</h2><a href="/rings/oval">Oval ring</a><a href="/">Home</a></body></html>')
    site.routes["/rings/oval"] = (200, {}, "<html><body><h1>Oval ring</h1><p>$1,299</p></body></html>")


def test_auth_envelope_and_balance(client):
    r = client.get("/v1/credits/balance")
    assert r.status_code == 401 and r.json()["success"] is False and r.json()["error"]["type"] == "auth_error"
    assert r.json()["request_id"].startswith("req_")
    k = api_key(client)
    body = client.get("/v1/credits/balance", headers=k).json()
    assert body["success"] and body["data"] == {"balance": 300} and body["credits_used"] == 0 and body["platform"] == "web"
    r = client.get("/v1/nope", headers=k)
    assert r.status_code == 404 and r.json()["error"]["type"] == "not_found"


def test_scrape_returns_markdown_and_costs_one_credit(client, site):
    shop(site)
    k = api_key(client)
    r = client.get("/v1/web/scrape", params={"url": site.base + "/"}, headers=k)
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["credits_used"] == 1 and b["credits_remaining"] == 299 and b["endpoint"] == "/v1/web/scrape"
    d = b["data"]
    assert d["title"] == "Lumen Jewels" and d["status"] == 200 and "# Fine jewellery" in d["markdown"]
    assert site.base + "/rings" in d["links"] and d["fetched_at"]


def test_scrape_refusals_are_free(client, site):
    shop(site)
    k = api_key(client)
    for url, msg in [(site.base + "/private/x", "robots.txt"), ("http://10.0.0.1/", "private"), ("ftp://x.com/a", "http(s)")]:
        r = client.get("/v1/web/scrape", params={"url": url}, headers=k)
        assert r.status_code == 400 and msg in r.json()["error"]["message"], r.text
    assert client.get("/v1/web/scrape", headers=k).json()["error"]["type"] == "validation_error"
    assert client.get("/v1/credits/balance", headers=k).json()["data"]["balance"] == 300


def test_scrape_without_credits_is_402(client, site):
    shop(site)
    k = api_key(client)
    set_credits("api@test.local", 0)
    r = client.get("/v1/web/scrape", params={"url": site.base + "/"}, headers=k)
    assert r.status_code == 402 and r.json()["error"]["type"] == "insufficient_credits"
    assert "/" not in site.hits  # nothing was fetched


def test_crawl_job_end_to_end(client, site, conn):
    shop(site)
    k = api_key(client)
    r = client.post("/v1/web/crawl", headers=k, json={"url": site.base + "/", "limit": 10, "max_depth": 2})
    assert r.status_code == 202, r.text
    job = r.json()["data"]
    assert job["status"] == "queued" and job["job_id"].startswith("crawl_") and job["progress"] == {"completed": 0, "total": 10}
    assert worker.run_once(conn) is True
    done = client.get(f"/v1/web/crawl/{job['job_id']}", headers=k).json()["data"]
    assert done["status"] == "completed", done
    urls = [p["url"] for p in done["result"]["pages"]]
    assert urls == [site.base + "/", site.base + "/rings", site.base + "/rings/oval"]  # robots + external links skipped
    assert [p["depth"] for p in done["result"]["pages"]] == [0, 1, 2]
    assert done["progress"] == {"completed": 3, "total": 3}
    assert client.get("/v1/credits/balance", headers=k).json()["data"]["balance"] == 297
    assert "/private/x" not in site.hits


def test_crawl_respects_limit_depth_and_paths(client, site, conn):
    shop(site)
    k = api_key(client)
    job = client.post("/v1/web/crawl", headers=k, json={"url": site.base + "/", "limit": 99, "max_depth": 1,
                                                         "exclude_paths": ["/rings/"]}).json()["data"]
    assert job["progress"]["total"] == 50  # limit clamped like the UI's crawler
    worker.run_once(conn)
    done = client.get(f"/v1/web/crawl/{job['job_id']}", headers=k).json()["data"]
    assert [p["url"] for p in done["result"]["pages"]] == [site.base + "/", site.base + "/rings"]


def test_crawl_stops_when_credits_run_out_and_keeps_partial_result(client, site, conn):
    shop(site)
    k = api_key(client)
    set_credits("api@test.local", 2)
    job = client.post("/v1/web/crawl", headers=k, json={"url": site.base + "/", "max_depth": 2}).json()["data"]
    worker.run_once(conn)
    done = client.get(f"/v1/web/crawl/{job['job_id']}", headers=k).json()["data"]
    assert done["status"] == "failed" and done["error"] == "insufficient credits"
    assert len(done["result"]["pages"]) == 2


def test_jobs_are_private_to_their_owner(client, site, conn):
    shop(site)
    k1, k2 = api_key(client, "one@test.local"), api_key(client, "two@test.local")
    job = client.post("/v1/web/crawl", headers=k1, json={"url": site.base + "/"}).json()["data"]
    r = client.get(f"/v1/web/crawl/{job['job_id']}", headers=k2)
    assert r.status_code == 404 and r.json()["error"]["type"] == "not_found"
    worker.run_once(conn)


def test_crawl_request_validation(client):
    k = api_key(client)
    for body in ({}, {"url": "not a url"}, {"url": "http://example.com", "limit": "lots"}):
        r = client.post("/v1/web/crawl", headers=k, json=body)
        assert r.status_code == 400 and r.json()["error"]["type"] == "validation_error", (body, r.text)
    r = client.post("/v1/web/crawl", headers=k, json={"url": "http://192.168.1.1/"})
    assert r.status_code == 400 and "private" in r.json()["error"]["message"]


def test_failed_page_is_recorded_not_fatal(client, site, conn):
    site.routes["/robots.txt"] = (404, {}, "")
    site.routes["/"] = (200, {}, '<a href="/img">pic</a><a href="/gone">x</a>')
    site.routes["/img"] = (200, {"content-type": "image/png"}, b"\x89PNG")
    k = api_key(client)
    job = client.post("/v1/web/crawl", headers=k, json={"url": site.base + "/"}).json()["data"]
    worker.run_once(conn)
    pages = client.get(f"/v1/web/crawl/{job['job_id']}", headers=k).json()["data"]["result"]["pages"]
    by_url = {p["url"].removeprefix(site.base): p for p in pages}
    assert "unsupported content-type image/png" in by_url["/img"]["error"]
    assert by_url["/gone"]["status"] == 404  # an HTTP error page is still a fetched page
