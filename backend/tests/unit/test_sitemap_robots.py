import gzip
from datetime import UTC, datetime

import pytest

from competitor_moves.crawler import sitemap
from competitor_moves.crawler.fetch import FetchError, Page
from competitor_moves.crawler.robots import Robots

NS = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'


def urlset(*items):
    body = "".join(f"<url><loc>{u}</loc>{f'<lastmod>{d}</lastmod>' if d else ''}</url>" for u, d in items)
    return f'<?xml version="1.0"?><urlset {NS}>{body}</urlset>'.encode()


def index(*items):
    body = "".join(f"<sitemap><loc>{u}</loc>{f'<lastmod>{d}</lastmod>' if d else ''}</sitemap>" for u, d in items)
    return f'<?xml version="1.0"?><sitemapindex {NS}>{body}</sitemapindex>'.encode()


def fake_fetch(files: dict):
    calls = []

    def fetch(url):
        calls.append(url)
        if url not in files:
            return Page(url, 404, {}, b"")
        return Page(url, 200, {}, files[url])
    fetch.calls = calls
    return fetch


def test_parse_urlset_and_lastmod_formats():
    sm = sitemap.parse(urlset(("https://s/a", "2026-10-01"), ("https://s/b", "2026-10-02T10:00:00Z"), ("https://s/c", None)))
    assert not sm.is_index
    assert [e.loc for e in sm.entries] == ["https://s/a", "https://s/b", "https://s/c"]
    assert sm.entries[0].lastmod == datetime(2026, 10, 1, tzinfo=UTC)
    assert sm.entries[1].lastmod == datetime(2026, 10, 2, 10, tzinfo=UTC)
    assert sm.entries[2].lastmod is None


def test_parse_gzip_and_reject_garbage():
    assert sitemap.parse(gzip.compress(urlset(("https://s/a", None)))).entries[0].loc == "https://s/a"
    for bad in (b"<html>nope</html>", b"not xml", gzip.compress(b"<html/>")):
        with pytest.raises(ValueError):
            sitemap.parse(bad)


def test_collect_skips_old_child_sitemaps_without_fetching_them():
    files = {
        "https://s/sitemap.xml": index(("https://s/old.xml", "2025-01-01"), ("https://s/new.xml.gz", "2026-10-08"),
                                       ("https://s/undated.xml", None)),
        "https://s/new.xml.gz": gzip.compress(urlset(("https://s/p/1", "2026-10-08"), ("https://s/p/2", "2026-01-01"))),
        "https://s/undated.xml": urlset(("https://s/p/3", None)),
    }
    f = fake_fetch(files)
    scan = sitemap.collect(f, ["https://s/sitemap.xml"], since=datetime(2026, 10, 1, tzinfo=UTC))
    assert "https://s/old.xml" not in f.calls
    assert sorted(e.loc for e in scan.urls) == ["https://s/p/1", "https://s/p/2", "https://s/p/3"]
    assert (scan.files_read, scan.files_skipped, scan.files_failed) == (3, 1, 0)


def test_collect_caps_files_and_estimates_the_rest():
    children = [(f"https://s/s{i}.xml", None) for i in range(10)]
    files = {"https://s/i.xml": index(*children)}
    files.update({u: urlset(*[(f"{u}/p{j}", None) for j in range(100)]) for u, _ in children})
    scan = sitemap.collect(fake_fetch(files), ["https://s/i.xml"], max_files=4)
    assert scan.truncated and len(scan.urls) == 300 and scan.files_unread == 7
    assert scan.est_total == 1000


def test_collect_counts_failures_and_survives_them():
    files = {"https://s/i.xml": index(("https://s/missing.xml", None), ("https://s/bad.xml", None)),
             "https://s/bad.xml": b"<oops"}
    scan = sitemap.collect(fake_fetch(files), ["https://s/i.xml"])
    assert scan.files_failed == 2 and scan.urls == []


def robots_from(status=200, body="", raise_error=False):
    def fetch(url):
        assert url == "https://s.com/robots.txt"
        if raise_error:
            raise FetchError("down")
        return Page(url, status, {"content-type": "text/plain"}, body.encode())
    return Robots.load("https://s.com", fetch, "RivalWatchBot/1.0")


def test_robots_rules_sitemaps_and_delay():
    r = robots_from(body="User-agent: *\nDisallow: /checkout\nCrawl-delay: 2\nSitemap: https://s.com/sitemap.xml\n")
    assert r.allowed("https://s.com/products/ring") and not r.allowed("https://s.com/checkout/1")
    assert r.sitemaps == ["https://s.com/sitemap.xml"] and r.crawl_delay == 2.0


def test_robots_bot_specific_block():
    r = robots_from(body="User-agent: RivalWatchBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n")
    assert not r.allowed("https://s.com/anything")


def test_robots_status_codes_follow_rfc9309():
    assert robots_from(status=404).allowed("https://s.com/x")
    assert not robots_from(status=503).allowed("https://s.com/x")
    assert not robots_from(raise_error=True).allowed("https://s.com/x")
