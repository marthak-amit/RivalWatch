"""Render pages that build their content with JavaScript, in headless Chrome via Playwright.

A rendered page runs the site's own scripts, and those scripts can make requests of their own. Every request the page
makes (documents, scripts, XHR/fetch, redirects) goes through the same SSRF guard as plain fetches, so a hostile site
can't make our browser reach internal addresses (e.g. the cloud metadata service) and copy the answer into its DOM.
Images, fonts and media are not loaded at all; service workers and websockets are blocked.
"""
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from ..core.ssrf import BlockedURL, check_url

_SKIP = {"image", "media", "font"}
_LOCAL_SCHEMES = ("data:", "blob:", "about:")
# ready = the page's own scripts have replaced the {{...}} templates in the heading and the product data
_READY = """() => {
  const h = document.querySelector('h1');
  const ld = [...document.querySelectorAll('script[type="application/ld+json"]')].map((s) => s.textContent).join(' ');
  const tpl = /\\{\\{\\s*[\\w.]+\\s*\\}\\}/;
  return !!h && !tpl.test(h.textContent) && !tpl.test(ld);
}"""


class BrowserUnavailable(Exception):
    pass


class Rendered:
    def __init__(self, url: str, status: int, html: str):
        self.url, self.status, self.html = url, status, html


class Renderer:
    """One headless browser for one crawl run; pages open and close per URL. Call close() when the run ends."""

    def __init__(self, user_agent: str, *, channel: str = "", timeout_sec: int = 30):
        self.user_agent, self.channel, self.timeout_ms = user_agent, channel, timeout_sec * 1000
        self._pw = self._browser = self._context = None
        self.blocked: list[str] = []  # requests the guard refused, for the run trace

    def _start(self) -> None:
        self._pw = sync_playwright().start()
        last = None
        for channel in dict.fromkeys([self.channel, "", "chrome"]):  # configured, then bundled Chromium, then Chrome
            try:
                self._browser = self._pw.chromium.launch(channel=channel or None, headless=True,
                                                         args=["--disable-dev-shm-usage"])
                break
            except PlaywrightError as e:
                last = e
        if self._browser is None:
            self._pw.stop()
            self._pw = None
            raise BrowserUnavailable(str(last).splitlines()[0][:200] if last else "no browser")
        self._context = self._browser.new_context(user_agent=self.user_agent, service_workers="block")
        self._context.route("**/*", self._guard)
        self._context.route_web_socket("**/*", lambda ws: ws.close())

    def _guard(self, route) -> None:
        req = route.request
        if req.resource_type in _SKIP:
            route.abort()
            return
        if req.url.startswith(_LOCAL_SCHEMES):
            route.continue_()
            return
        try:
            check_url(req.url)
        except BlockedURL:
            self.blocked.append(req.url[:200])
            route.abort("blockedbyclient")
            return
        route.continue_()

    def render(self, url: str) -> Rendered:
        """Raises BlockedURL for the page itself, BrowserUnavailable, or PlaywrightError on navigation failure."""
        check_url(url)
        if self._context is None:
            self._start()
        page = self._context.new_page()
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            try:
                page.wait_for_function(_READY, timeout=min(10_000, self.timeout_ms))
            except PlaywrightTimeout:
                pass  # take what rendered; the extractor refuses anything still templated
            return Rendered(page.url, response.status if response else 0, page.content())
        finally:
            page.close()

    def close(self) -> None:
        for closer in (self._context, self._browser):
            try:
                if closer is not None:
                    closer.close()
            except PlaywrightError:
                pass
        if self._pw is not None:
            self._pw.stop()
        self._pw = self._browser = self._context = None
