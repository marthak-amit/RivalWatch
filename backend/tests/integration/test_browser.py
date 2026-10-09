"""Headless rendering of JavaScript-built pages, with the SSRF guard on every request the page makes."""
import pytest

from competitor_moves.crawler import extract
from competitor_moves.crawler.browser import BrowserUnavailable, Renderer

PAGE = """<html><head><script type="application/ld+json">{"@type":"Product","name":"{{name}}","offers":{"price":"{{price}}"}}</script>
</head><body><h1>{{name}}</h1><script>
fetch('http://127.0.0.1:9/internal-admin').catch(() => {});
fetch('http://169.254.169.254/latest/meta-data/iam/').catch(() => {});
setTimeout(() => {
  document.querySelector('h1').textContent = 'Halo Ring';
  document.querySelector('script[type="application/ld+json"]').textContent =
    JSON.stringify({"@type": "Product", "name": "Halo Ring", "sku": "HR-1", "offers": {"price": "1500", "priceCurrency": "INR"}});
}, 200);
</script></body></html>"""


@pytest.fixture
def renderer():
    r = Renderer("RivalWatchBot/1.0", timeout_sec=20)
    yield r
    r.close()


def test_renders_templates_and_blocks_internal_requests(site, renderer):
    site.routes["/p"] = (200, {}, PAGE)
    assert extract.needs_browser(PAGE) and extract.extract_products(PAGE, site.base + "/p") == []
    try:
        page = renderer.render(site.base + "/p")
    except BrowserUnavailable as e:
        pytest.skip(f"no browser on this machine: {e}")
    [p] = extract.extract_products(page.html, page.url)
    assert (p["title"], str(p["price"]), p["currency"], p["sku"]) == ("Halo Ring", "1500", "INR", "HR-1")
    assert any("127.0.0.1:9" in u for u in renderer.blocked) and any("169.254.169.254" in u for u in renderer.blocked)


def test_the_page_itself_must_be_public(renderer):
    from competitor_moves.core.ssrf import BlockedURL
    with pytest.raises(BlockedURL):
        renderer.render("http://10.0.0.1/")
