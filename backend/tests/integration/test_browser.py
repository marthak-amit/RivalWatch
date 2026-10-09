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


@pytest.fixture
def browser_server():
    """A Playwright browser server like the `browser` image runs on the user's computer, here on a free local port."""
    import socket
    import subprocess
    import sys
    import time

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    secret = "test-secret-path-0123456789"
    proc = subprocess.Popen([sys.executable, "-m", "playwright", "run-server", "--host", "127.0.0.1", "--port", str(port),
                             "--path", f"/{secret}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    yield f"ws://127.0.0.1:{port}/{secret}"
    proc.terminate()
    proc.wait(10)


def test_a_browser_on_another_machine_renders_and_the_guard_still_holds(site, browser_server):
    site.routes["/p"] = (200, {}, PAGE)
    r = Renderer("RivalWatchBot/1.0", timeout_sec=20, ws_url=browser_server)
    try:
        page = r.render(site.base + "/p")
    except BrowserUnavailable as e:
        pytest.skip(f"the browser server couldn't start a browser on this machine: {e}")
    finally:
        r.close()
    [p] = extract.extract_products(page.html, page.url)
    assert p["title"] == "Halo Ring"
    assert any("127.0.0.1:9" in u for u in r.blocked) and any("169.254.169.254" in u for u in r.blocked)


def test_an_unreachable_or_wrong_browser_server_never_shows_its_secret(site, browser_server):
    site.routes["/p"] = (200, {}, PAGE)
    for url in (browser_server.rsplit("/", 1)[0] + "/wrong-secret-abcdef", "ws://127.0.0.1:9/secret-path-abcdef"):
        r = Renderer("RivalWatchBot/1.0", timeout_sec=3, ws_url=url)
        with pytest.raises(BrowserUnavailable) as e:
            r.render(site.base + "/p")
        msg = str(e.value)
        assert msg.startswith("browser server 127.0.0.1:") and "secret" not in msg, msg
