import pytest

from competitor_moves.core.ssrf import BlockedURL
from competitor_moves.crawler.fetch import FetchError, fetch

UA = "RivalWatchBot/1.0"


def test_fetch_follows_redirects_and_decodes(site):
    site.routes["/old"] = (301, {"location": "/new"}, "")
    site.routes["/new"] = (200, {"content-type": "text/html; charset=iso-8859-1"}, "<p>caf\xe9</p>".encode("latin-1"))
    page = fetch(site.base + "/old", user_agent=UA)
    assert page.url == site.base + "/new" and page.status == 200 and "café" in page.text()


def test_redirect_to_private_address_is_blocked(site):
    site.routes["/go"] = (302, {"location": "http://127.0.0.1:1/admin"}, "")  # different port: not allowed
    with pytest.raises(BlockedURL):
        fetch(site.base + "/go", user_agent=UA)
    site.routes["/meta"] = (302, {"location": "http://169.254.169.254/latest/meta-data"}, "")
    with pytest.raises(BlockedURL):
        fetch(site.base + "/meta", user_agent=UA)


def test_redirect_loop_and_error_status(site):
    site.routes["/a"] = (302, {"location": "/b"}, "")
    site.routes["/b"] = (302, {"location": "/a"}, "")
    with pytest.raises(FetchError):
        fetch(site.base + "/a", user_agent=UA)
    assert fetch(site.base + "/missing", user_agent=UA).status == 404


def test_network_failure_is_a_fetch_error(monkeypatch):
    from competitor_moves.core import ssrf
    monkeypatch.setattr(ssrf, "ALLOW_LOCAL", {"127.0.0.1:9"})
    with pytest.raises(FetchError):
        fetch("http://127.0.0.1:9/", user_agent=UA, timeout=3, retries=1)


def test_stay_on_stops_at_an_off_site_redirect_without_following_it(site, monkeypatch):
    from competitor_moves.core import ssrf
    other = "127.0.0.2:9"  # would be another site; must never be contacted
    monkeypatch.setattr(ssrf, "ALLOW_LOCAL", {site.base.removeprefix("http://"), other})
    site.routes["/out"] = (302, {"location": f"http://{other}/landing"}, "")
    site.routes["/in"] = (302, {"location": "/here"}, "")
    site.routes["/here"] = (200, {}, "ok")
    with pytest.raises(FetchError, match="redirected off-site"):
        fetch(site.base + "/out", user_agent=UA, stay_on=site.base + "/", timeout=3, retries=1)
    assert fetch(site.base + "/in", user_agent=UA, stay_on=site.base + "/").url == site.base + "/here"
