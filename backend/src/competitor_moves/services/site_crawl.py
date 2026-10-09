"""One crawl of one website: the RunContext shared by tools and nodes, and the six crawl tools (agent spec §1.6).

The tools are plain functions so the LLM agent and the fixed fallback plan run exactly the same code. Every limit
(page budget, product limit, credits, robots.txt, same-site, time) is enforced here, never by a prompt. Tools never
raise to the agent: they return a few lines of text ending with the status line.
"""
import json
import math
import random
import re
import time
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import wraps
from urllib.parse import urljoin, urlsplit

from scrapling.parser import Selector

from ..config import get_settings
from ..core.ssrf import BlockedURL
from ..crawler import extract, platforms, sitemap
from ..crawler.fetch import ACCEPT_HTML, FetchError, Page
from ..crawler.fetch import fetch as http_fetch
from ..crawler.robots import Robots
from ..db.repositories import credits, site_data
from ..db.repositories import sites as sites_repo
from . import change_detection as cd

WINDOW_DAYS = 7
MAX_SITEMAP_FILES = 60
ACCEPT_JSON = "application/json,*/*;q=0.5"
ACCEPT_XML = "application/xml,text/xml;q=0.9,*/*;q=0.5"
FEED_PAGE_SIZE = {"shopify": 250, "woocommerce": 100}
_NOT_PRODUCT = re.compile(r"/(cart|checkout|account|login|register|search|wishlist|compare|pages|blogs?|collections|"
                          r"categor(y|ies)|contact|about|faq|policies|policy)(/|$)|[?&](page|sort|filter|order)=", re.IGNORECASE)


class ToolStop(Exception):
    """The current tool ends and reports why: a hard stop (budget, credits, blocked, time) or the product limit."""


class SkipURL(FetchError):
    """This URL can't be fetched (robots.txt, off-site, private address, network error); move on."""


@dataclass
class RunContext:
    conn: object
    site: dict
    started: datetime
    max_products: int
    page_budget: int
    deadline: float
    prev_run: dict | None
    known_products: int
    cfg: dict = field(default_factory=dict)          # effective crawl settings (site overrides on server defaults)
    categories: list = field(default_factory=list)  # [] = whole site, else menu categories {name, url} to stay inside
    sort: str = "relevance"                          # which products fill the product limit first
    run_id: int = 0
    # progress
    pages_used: int = 0
    pages_failed: int = 0
    products_seen: set = field(default_factory=set)
    fetched_urls: set = field(default_factory=set)
    stop_reason: str | None = None  # hard stop: no more fetching at all (BUDGET REACHED, NO CREDITS, BLOCKED, TIME LIMIT)
    limit_hit: bool = False         # product limit reached: no more products, but other checks may still fetch
    full_coverage: bool = False
    est_total: int | None = None
    credits_left: int | None = None
    # what the run found out
    robots: Robots | None = None
    delay: float = 0.5
    platform: str | None = None
    currency: str | None = None  # the shop's own currency, for feeds whose prices carry none (Shopify)
    has_feed: bool = False
    feed_first: list | None = None
    sitemaps: list = field(default_factory=list)
    home: dict | None = None
    nav: list = field(default_factory=list)
    inspected: bool = False
    queues: dict = field(default_factory=lambda: {"recent": deque(), "all": deque(), "sample": deque()})
    url_category: dict = field(default_factory=dict)
    url_lastmod: dict = field(default_factory=dict)  # sitemap lastmod per URL, used as the product's source date
    events: list = field(default_factory=list)
    trace: list = field(default_factory=list)
    # pages built by JavaScript and pages read by the AI
    renderer: object = None
    browser_pages_used: int = 0
    browser_unavailable: str | None = None
    ai_extract: Callable | None = None  # (html, url) -> product | None, set by the graph when a model is configured
    ai_used: int = 0
    # filled in by the graph nodes
    groups: list | None = None
    models: dict = field(default_factory=dict)
    outcome: str | None = None
    summary: str | None = None
    _last_fetch_at: float = 0.0
    _blocks_in_a_row: int = 0

    # ---- set-up -------------------------------------------------------------------------------------------
    @classmethod
    def load(cls, conn, site_id: int) -> "RunContext":
        s = get_settings()
        site = sites_repo.for_crawl(conn, site_id)
        if site is None:
            raise LookupError(f"site {site_id} not found")
        from .workspaces import crawl_settings
        cfg = crawl_settings(site["crawl_settings"])
        max_products = min(site["max_products"] or s.default_max_products, s.max_products_cap)
        return cls(conn=conn, site=site, started=datetime.now(UTC), max_products=max_products,
                   page_budget=site["page_budget"] or s.page_budget, deadline=time.monotonic() + cfg["time_limit_sec"],
                   prev_run=site_data.last_run(conn, site_id), known_products=site_data.product_count(conn, site_id),
                   delay=cfg["delay_sec"], cfg=cfg, categories=list(site["categories"] or []), sort=site["sort"])

    @property
    def site_id(self) -> int:
        return self.site["id"]

    @property
    def origin(self) -> str:
        p = urlsplit(self.site["url"])
        return f"{p.scheme}://{p.netloc}"

    @property
    def run_type(self) -> str:
        return "incremental" if self.known_products else "first"

    @property
    def window_start(self) -> datetime:
        return self.started - timedelta(days=WINDOW_DAYS)

    @property
    def prev_full_coverage(self) -> bool:
        return bool(self.prev_run and self.prev_run["full_coverage"])

    @property
    def prev_home(self) -> dict | None:
        return self.prev_run["home"] if self.prev_run else None

    @property
    def days_since_last_run(self) -> int:
        if not self.prev_run or not self.prev_run["finished_at"]:
            return WINDOW_DAYS
        return max(1, math.ceil((self.started - self.prev_run["finished_at"]).total_seconds() / 86400))

    def in_scope(self, category: str | None) -> bool:
        if not self.categories:
            return True
        names = {c["name"].strip().lower() for c in self.categories}
        return bool(category) and category.strip().lower() in names

    @property
    def scope_text(self) -> str:
        return ("only these menu categories: " + ", ".join(f'"{c["name"]}"' for c in self.categories)
                if self.categories else "the whole site")

    @property
    def charges_credits(self) -> bool:
        return self.site["owner_user_id"] is not None

    # ---- limits and fetching ------------------------------------------------------------------------------
    def stop(self, reason: str):
        self.stop_reason = self.stop_reason or reason
        raise ToolStop(self.stop_reason)

    @property
    def stop_label(self) -> str | None:
        return self.stop_reason or ("LIMIT REACHED" if self.limit_hit else None)

    def status_line(self) -> str:
        left = self.credits_left if self.credits_left is not None else (
            credits.balance(self.conn, self.site["owner_user_id"]) if self.charges_credits else "n/a")
        line = f"status: pages {self.pages_used}/{self.page_budget} | products {len(self.products_seen)}/{self.max_products} | credits {left}"
        return line + (f" | {self.stop_label}" if self.stop_label else "")

    def _take_page(self, url: str, check_robots: bool) -> None:
        """Every limit a request must pass before it is made, then count it (page budget, credit, politeness delay)."""
        if self.stop_reason:
            raise ToolStop(self.stop_reason)
        if time.monotonic() > self.deadline:
            self.stop("TIME LIMIT")
        if not extract.same_site(url, self.site["url"]):
            raise SkipURL(f"off-site URL {url}")
        if check_robots and self.robots and not self.robots.allowed(url):
            raise SkipURL("disallowed by robots.txt")
        if self.pages_used >= self.page_budget:
            self.stop("BUDGET REACHED")
        if self.charges_credits:
            try:
                self.credits_left = credits.charge(self.conn, self.site["owner_user_id"], 1)
            except credits.InsufficientCredits:
                self.stop("NO CREDITS")
        wait = self._last_fetch_at + self.delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self.pages_used += 1
        self._last_fetch_at = time.monotonic()
        if self.pages_used % 10 == 0 and self.run_id:  # live progress for the dashboard while a crawl runs
            site_data.progress(self.conn, self.site_id, self.run_id, self.pages_used, len(self.products_seen))

    def fetch(self, url: str, *, accept: str = ACCEPT_HTML, check_robots: bool = True) -> Page:
        self._take_page(url, check_robots)
        s = get_settings()
        try:
            page = http_fetch(url, user_agent=s.user_agent, timeout=s.fetch_timeout_sec, accept=accept,
                              stay_on=self.site["url"])
        except (FetchError, BlockedURL) as e:
            self.pages_failed += 1
            raise SkipURL(str(e)) from None
        if not extract.same_site(page.url, self.site["url"]):
            self.pages_failed += 1
            raise SkipURL(f"redirected off-site to {page.url}")
        if page.status in (403, 429) or (page.status == 503 and _challenge(page)):
            self._blocks_in_a_row += 1
            self.pages_failed += 1
            if self._blocks_in_a_row >= 3:
                self.stop("BLOCKED")
            raise SkipURL(f"HTTP {page.status}")
        self._blocks_in_a_row = 0
        if page.status >= 400:
            self.pages_failed += 1
        return page

    def render(self, url: str) -> Page | None:
        """The page as a browser sees it, for pages built by JavaScript. None when rendering is off, capped or
        unavailable. Counts as a page (budget and credit) like any fetch."""
        if self.cfg.get("browser") != "auto" or self.browser_unavailable:
            return None
        if self.browser_pages_used >= self.cfg.get("browser_pages", 0):
            return None
        try:  # the browser stack is only installed in the worker image
            from playwright.sync_api import Error as PlaywrightError

            from ..crawler.browser import BrowserUnavailable, Renderer
        except ImportError:
            self.browser_unavailable = "playwright is not installed in this image"
            self.trace.append({"node": "browser", "error": "no browser available: playwright is not installed"})
            return None
        self._take_page(url, check_robots=True)
        s = get_settings()
        try:
            if self.renderer is None:
                self.renderer = Renderer(s.user_agent, channel=s.browser_channel, timeout_sec=s.browser_timeout_sec,
                                         ws_url=s.browser_ws_url)
            r = self.renderer.render(url)
        except BrowserUnavailable as e:
            self.browser_unavailable = str(e)
            self.trace.append({"node": "browser", "error": f"no browser available: {e}"[:300]})
            return None
        except (BlockedURL, PlaywrightError) as e:
            self.pages_failed += 1
            raise SkipURL(f"render failed: {str(e).splitlines()[0][:150]}") from None
        self.browser_pages_used += 1
        if not extract.same_site(r.url, self.site["url"]):
            self.pages_failed += 1
            raise SkipURL(f"redirected off-site to {r.url}")
        return Page(url=r.url, status=r.status, headers={"content-type": "text/html; charset=utf-8"}, body=r.html.encode())

    def close(self) -> None:
        if self.renderer is not None:
            if self.renderer.blocked:
                self.trace.append({"node": "browser", "blocked_requests": self.renderer.blocked[:20]})
            self.renderer.close()
            self.renderer = None

    def record(self, item: dict) -> list[dict]:
        """Store one product (and its events) unless the product limit is reached."""
        if self.stop_reason:
            raise ToolStop(self.stop_reason)
        if self.limit_hit:
            raise ToolStop("LIMIT REACHED")
        if not item.get("url") or item["url"] in self.products_seen or not extract.same_site(item["url"], self.site["url"]):
            return []
        if not item.get("category"):
            item = {**item, "category": self.url_category.get(item["url"])}
        if not item.get("currency") and self.currency:
            item = {**item, "currency": self.currency}
        evs = cd.record_product(self.conn, self.site_id, self.run_id, item, now=self.started,
                                window_start=self.window_start, prev_full_coverage=self.prev_full_coverage)
        self.products_seen.add(item["url"])
        self.events += evs
        if len(self.products_seen) >= self.max_products:
            self.limit_hit = True
        return evs

    def add_events(self, events: list[dict], *, source_url: str | None = None, product: dict | None = None) -> None:
        pid = product["id"] if product else None
        ids = cd.store_events(self.conn, self.site_id, self.run_id, events, pid, product=product, source_url=source_url)
        self.events += [{**e, "id": i, "product_id": pid} for e, i in zip(events, ids, strict=True)]


def _challenge(page: Page) -> bool:
    head = page.body[:5000].lower()
    return b"captcha" in head or b"cf-chl" in head or b"challenge-platform" in head


def tool(fn: Callable) -> Callable:
    """Record each tool call in the run trace; turn a hard stop into a normal result."""
    @wraps(fn)
    def run(ctx: RunContext, *args, **kwargs) -> str:
        t0, pages0 = time.monotonic(), ctx.pages_used
        try:
            out = fn(ctx, *args, **kwargs)
        except ToolStop:
            out = f"stopped: {ctx.stop_label}\n{ctx.status_line()}"
        ctx.trace.append({"tool": fn.__name__, "args": kwargs or list(args), "result": out.splitlines()[0][:200],
                          "pages": ctx.pages_used - pages0, "seconds": round(time.monotonic() - t0, 2)})
        return out
    return run


def tally(evs: list[dict]) -> str:
    c = Counter(e["type"] for e in evs)
    down, up = c["price_drop"], c["price_rise"]
    return (f"new {c['new_product']} | price changes {down + up} ({down} down, {up} up) | on sale {c['on_sale']} | "
            f"out of stock {c['out_of_stock']}")


def _feed_items(platform: str | None, data) -> list | None:
    if platform == "shopify" and isinstance(data, dict) and isinstance(data.get("products"), list):
        return data["products"]
    if platform == "woocommerce" and isinstance(data, list):
        return data
    return None


def _quote(xs, n=12) -> str:
    return ", ".join(f'"{x[:60]}"' for x in xs[:n]) + (", …" if len(xs) > n else "")


# ---- the six tools ------------------------------------------------------------------------------------------

@tool
def inspect_site(ctx: RunContext) -> str:
    if ctx.inspected:
        return f"ERROR: inspect_site was already called this run\n{ctx.status_line()}"
    ctx.inspected = True
    ua = get_settings().user_agent
    ctx.robots = Robots.load(ctx.origin, lambda u: ctx.fetch(u, accept="text/plain,*/*;q=0.5", check_robots=False), ua)
    ctx.delay = max(ctx.delay, min(ctx.robots.crawl_delay or 0, 10))
    if not ctx.robots.allowed(ctx.site["url"]):
        ctx.stop_reason = "BLOCKED"
        return f"robots.txt disallows crawling this site; nothing more can be fetched\n{ctx.status_line()}"
    try:
        home = ctx.fetch(ctx.site["url"])
    except SkipURL as e:
        return f"ERROR: homepage could not be fetched ({e})\n{ctx.status_line()}"
    if home.status >= 400:
        return f"ERROR: homepage answered HTTP {home.status}\n{ctx.status_line()}"
    html = home.text()
    ctx.platform = platforms.detect(html)
    ctx.home = extract.page_meta(html, home.url)
    ctx.nav = ctx.home["nav"]
    ctx.home["profile"] = extract.profile(html, home.url, ctx.nav)
    if ctx.run_id:
        site_data.save_home(ctx.conn, ctx.site_id, ctx.run_id, ctx.home)
    if (feed := platforms.feed_url(ctx.platform, ctx.origin, 1)) and ctx.robots.allowed(feed):
        try:
            page = ctx.fetch(feed, accept=ACCEPT_JSON)
            ctx.feed_first = _feed_items(ctx.platform, json.loads(page.body)) if page.status == 200 else None
        except (SkipURL, ValueError):
            ctx.feed_first = None
        ctx.has_feed = ctx.feed_first is not None
    ctx.sitemaps = ctx.robots.sitemaps or [ctx.origin + "/sitemap.xml"]
    evs = [e for e in cd.home_events(ctx.prev_home, ctx.home, now=ctx.started)
           if e["type"] != "new_category" or not site_data.recent_event_exists(
               ctx.conn, ctx.site_id, "new_category", e["category"], ctx.window_start)]
    ctx.add_events(evs, source_url=home.url)
    changes = ([f'new promotion "{e["after"]["text"][:80]}"' for e in evs if e["type"] == "promo_started"]
               + [f'promotion ended "{e["before"]["text"][:80]}"' for e in evs if e["type"] == "promo_ended"]
               + [f'new menu item "{e["category"]}"' for e in evs if e["type"] == "new_category"])
    first_look = ctx.prev_home is None
    return "\n".join([
        (f"platform: {ctx.platform} | product feed: {'yes' if ctx.has_feed else 'no'} | "
         f"sitemaps: {len(ctx.sitemaps)} | robots: allowed"),
        f"menu categories ({len(ctx.nav)}): {_quote([n['name'] for n in ctx.nav]) or 'none found'}",
        "homepage changes: " + ("first look (baseline)" if first_look else "; ".join(changes) or "none"),
        ctx.status_line()])


@tool
def fetch_product_feed(ctx: RunContext, max_items: int | None = None) -> str:
    if not ctx.inspected:
        return f"ERROR: call inspect_site first\n{ctx.status_line()}"
    if not ctx.has_feed:
        return f"ERROR: no product feed on this site; use read_sitemap\n{ctx.status_line()}"
    remaining = ctx.max_products - len(ctx.products_seen)
    target = min(max_items, remaining) if max_items else remaining
    sources, skipped = _feed_sources(ctx)
    # page 1 of the whole-site feed was already fetched by inspect_site's probe
    before, requests = len(ctx.events), 1 if ctx.feed_first is not None and sources[0][0] is None else 0
    # site order and no scope: stop reading once the limit is filled; otherwise read more so the sort is real
    cap = target if ctx.sort == "relevance" and not ctx.categories else min(max(target * 5, 250), 2000)
    candidates: dict[str, dict] = {}
    whole_catalog = not ctx.categories
    size = FEED_PAGE_SIZE[ctx.platform]
    try:
        if ctx.platform == "shopify" and ctx.currency is None:
            ctx.currency = _shopify_currency(ctx)
        for label, url_for in sources:
            page_no, items = 1, (ctx.feed_first if label is None else None)
            ended = False
            while len(candidates) < cap:
                if items is None:
                    page = ctx.fetch(url_for(page_no), accept=ACCEPT_JSON)
                    requests += 1
                    try:
                        items = _feed_items(ctx.platform, json.loads(page.body)) if page.status == 200 else None
                    except ValueError:
                        items = None
                    if items is None:
                        break
                if not items:
                    ended = True
                    break
                for raw in items:
                    item = extract.shopify_product(raw, ctx.origin) if ctx.platform == "shopify" else extract.woo_product(raw)
                    if label and not item.get("category"):
                        item["category"] = label
                    candidates.setdefault(item["url"], {**item, "source_url": url_for(page_no)})
                if len(items) < size:
                    ended = True
                    break
                page_no, items = page_no + 1, None
            whole_catalog = whole_catalog and ended
            if label is None:
                ctx.feed_first = None
    except (ToolStop, SkipURL):
        whole_catalog = False
    chosen = list(candidates.values())
    if ctx.categories and ctx.platform != "shopify":  # no per-category feed: keep the scoped categories only
        chosen = [c for c in chosen if ctx.in_scope(c.get("category"))]
    chosen = order(chosen, ctx.sort)[:target]
    stored, meta_pages = 0, 0
    try:
        for item in chosen:
            ctx.record(item)
            stored += 1
        if whole_catalog and len(candidates) <= target and not ctx.limit_hit:  # every product of the site stored
            ctx.full_coverage, ctx.est_total = True, len(ctx.products_seen)
        if ctx.platform == "shopify":
            _shopify_new_collections(ctx)
        meta_pages = _fill_meta(ctx, [item["url"] for item in chosen])  # last: one page per product, budget permitting
    except (ToolStop, SkipURL):
        pass
    if whole_catalog and not ctx.est_total:
        ctx.est_total = len(candidates)
    evs = ctx.events[before:]
    cats = Counter(e.get("category") for e in evs if e.get("category"))
    scope = f" from {_quote([c['name'] for c in ctx.categories], 6)}" if ctx.categories else ""
    return "\n".join([
        (f"feed read: {len(candidates)} products in {requests} request(s){scope}, kept {stored} ({SORT_LABELS[ctx.sort]})"
         f"{' (whole catalog)' if ctx.full_coverage else ''} | {tally(evs)}"
         + (f" | product pages read for meta tags: {meta_pages}" if meta_pages else "")),
        "categories with changes: " + (", ".join(f'"{c}" {n}' for c, n in cats.most_common(5)) or "none")
        + (f" | no feed for: {_quote(skipped, 4)}" if skipped else ""),
        ctx.status_line()])


def _fill_meta(ctx: RunContext, urls: list[str]) -> int:
    """Feeds carry no page meta tags: open each product page once (and again when stale) to store them."""
    s = get_settings()
    if not s.product_page_meta or not urls:
        return 0
    due = site_data.urls_needing_meta(ctx.conn, ctx.site_id, urls, ctx.started - timedelta(days=s.meta_refresh_days))
    read = 0
    for url in due:
        try:
            page = ctx.fetch(url)
        except SkipURL:
            continue
        except ToolStop:  # out of budget/credits: the rest wait for the next run
            break
        if page.status == 200:
            ctx.events += cd.record_meta(ctx.conn, ctx.site_id, ctx.run_id, url, extract.page_tags(page.text()),
                                         now=ctx.started)
            read += 1
    return read


def _feed_sources(ctx: RunContext) -> tuple[list[tuple[str | None, Callable[[int], str]]], list[str]]:
    """(label, page -> URL) for each feed to read, and the scoped categories that have no feed of their own."""
    if not ctx.categories or ctx.platform != "shopify":
        return [(None, lambda n: platforms.feed_url(ctx.platform, ctx.origin, n))], []
    sources, skipped = [], []
    for c in ctx.categories:
        m = re.search(r"/collections/([^/?#]+)", c.get("url") or "")
        if m:
            handle = m.group(1)
            sources.append((c["name"], lambda n, h=handle: f"{ctx.origin}/collections/{h}/products.json?limit=250&page={n}"))
        else:
            skipped.append(c["name"])
    return sources, skipped


SORT_LABELS = {"relevance": "site order", "newest": "newest first", "price_asc": "lowest price first",
               "price_desc": "highest price first", "discount": "biggest discount first"}


def _discount(p: dict) -> float:
    price, was = p.get("price"), p.get("compare_at_price")
    return float((was - price) / was) if price is not None and was and was > price else 0.0


def order(items: list[dict], sort: str) -> list[dict]:
    """Which products fill the product limit first. Unknown values go last; ties keep the site's order."""
    if sort == "newest":
        return sorted(items, key=lambda p: (p.get("source_date") is None, -(p["source_date"].timestamp() if p.get("source_date") else 0)))
    if sort == "price_asc":
        return sorted(items, key=lambda p: (p.get("price") is None, p.get("price") or 0))
    if sort == "price_desc":
        return sorted(items, key=lambda p: (p.get("price") is None, -(p.get("price") or 0)))
    if sort == "discount":
        return sorted(items, key=lambda p: -_discount(p))
    return list(items)


def _shopify_currency(ctx: RunContext) -> str | None:
    """Shopify's /meta.json names the shop currency, which is what products.json prices are in."""
    try:
        page = ctx.fetch(f"{ctx.origin}/meta.json", accept=ACCEPT_JSON)
        code = json.loads(page.body).get("currency") if page.status == 200 else None
    except (SkipURL, ValueError, AttributeError):
        return None
    return code if isinstance(code, str) and re.fullmatch(r"[A-Z]{3}", code) else None


def _shopify_new_collections(ctx: RunContext) -> None:
    src = f"{ctx.origin}/collections.json?limit=250"
    page = ctx.fetch(src, accept=ACCEPT_JSON)
    try:
        cols = json.loads(page.body).get("collections") if page.status == 200 else None
    except ValueError:
        cols = None
    evs = []
    for c in cols or []:
        published = sitemap.parse_lastmod(c.get("published_at"))
        name = (c.get("title") or "").strip()
        if not name or not published or published < ctx.window_start:
            continue
        if any(e["type"] == "new_category" and e["category"] == name for e in ctx.events):
            continue
        if site_data.recent_event_exists(ctx.conn, ctx.site_id, "new_category", name, ctx.window_start):
            continue
        evs.append({"type": "new_category", "category": name, "before": None,
                    "after": {"name": name, "url": f"{ctx.origin}/collections/{c.get('handle')}"}, "occurred_at": published})
    ctx.add_events(evs, source_url=src)


@tool
def read_sitemap(ctx: RunContext, changed_since_days: int | None = None) -> str:
    if not ctx.inspected:
        return f"ERROR: call inspect_site first\n{ctx.status_line()}"
    if changed_since_days is not None:
        changed_since_days = min(max(int(changed_since_days), 1), 365)
    since = ctx.started - timedelta(days=changed_since_days) if changed_since_days else None
    scan = sitemap.collect(lambda u: ctx.fetch(u, accept=ACCEPT_XML, check_robots=False), ctx.sitemaps, since=since,
                           max_files=MAX_SITEMAP_FILES, keep_file=lambda u: extract.same_site(u, ctx.site["url"]))
    entries = scan.urls
    landing = {e.loc for e in entries if cd.is_landing_page(e.loc, e.source)}
    has_product_files = any("product" in e.source.rsplit("/", 1)[-1].lower() for e in entries)
    candidates = [e for e in entries if e.loc not in landing and e.loc.rstrip("/") != ctx.origin
                  and (not has_product_files or "product" in e.source.rsplit("/", 1)[-1].lower())
                  and ctx.robots.allowed(e.loc)]
    ctx.url_lastmod.update({e.loc: e.lastmod for e in candidates if e.lastmod})
    if ctx.categories:  # sitemaps don't say which category a URL is in: products come from the scoped listings
        candidates = []
    window = since or ctx.window_start
    dated = [e for e in candidates if e.lastmod]
    fresh = sum(1 for e in dated if e.lastmod >= ctx.started - timedelta(days=2))
    dates_ok = not (len(dated) >= 20 and fresh / len(dated) > 0.8)
    if not dates_ok:  # nearly every URL "changed" in the last two days: the sitemap stamps today's date on everything
        for e in dated:
            ctx.url_lastmod.pop(e.loc, None)
    recent = [e.loc for e in candidates if e.lastmod and e.lastmod >= window] if dates_ok else []
    known = site_data.known_urls(ctx.conn, ctx.site_id)
    unseen = [e.loc for e in candidates if e.loc not in known]
    random.Random(ctx.run_id).shuffle(unseen)
    ctx.queues = {"recent": deque(recent), "all": deque(e.loc for e in candidates),
                  "sample": deque(unseen[: 2 * ctx.max_products])}
    share = len(candidates) / len(entries) if entries else 0
    ctx.est_total = round(scan.est_total * share) if scan.truncated else len(candidates)
    known_landing = site_data.landing_pages(ctx.conn, ctx.site_id)
    complete = since is None and not scan.truncated and scan.files_failed == 0
    ctx.add_events(cd.page_events(known_landing, landing, complete=complete, first_run=not known_landing, now=ctx.started),
                   source_url=ctx.sitemaps[0] if ctx.sitemaps else None)
    if landing:
        site_data.save_landing_pages(ctx.conn, ctx.site_id, landing, complete=complete)
    new_pages = sum(1 for e in ctx.events if e["type"] == "new_page")
    examples = _quote([urlsplit(u).path for u in list(ctx.queues["recent"] or ctx.queues["all"])[:2]], 2)
    window_note = f"changed in last {changed_since_days} day(s)" if changed_since_days else "changed in last 7 days"
    stopped = f" | stopped: {ctx.stop_label}" if ctx.stop_label else ""
    return "\n".join([
        (f"sitemap: {scan.files_read} files read, {scan.files_skipped} skipped as unchanged, {scan.files_failed} failed | "
         f"~{ctx.est_total} product URLs | {window_note}: {len(recent)}{stopped}"
         + ("" if dates_ok else f" | sitemap dates ignored: {round(100 * fresh / len(dated))}% of URLs claim a change in 2 days")),
        f"queued: recent {len(ctx.queues['recent'])}, all {len(ctx.queues['all'])}, sample {len(ctx.queues['sample'])}"
        + (f" | examples: {examples}" if examples else ""),
        f"landing pages: {len(landing)} tracked, {new_pages} new"
        + (" | scope is limited to categories: products are not queued from the sitemap" if ctx.categories else ""),
        ctx.status_line()])


@tool
def fetch_category_listing(ctx: RunContext, category: str, max_pages: int = 2) -> str:
    nav = ctx.nav or (ctx.prev_home or {}).get("nav") or []
    if not ctx.in_scope(category):
        return (f'ERROR: "{category}" is outside this site\'s scope. Allowed: '
                f"{_quote([c['name'] for c in ctx.categories], 8)}\n{ctx.status_line()}")
    scoped = next((c for c in ctx.categories if c["name"].strip().lower() == (category or "").strip().lower()), None)
    match = scoped or next((n for n in nav if n["name"].strip().lower() == (category or "").strip().lower()), None)
    if match is None:
        return (f'ERROR: unknown category "{category}". Copy a name from inspect_site, e.g. '
                f"{_quote([n['name'] for n in nav], 8) or '(no menu found)'}\n{ctx.status_line()}")
    max_pages = min(max(int(max_pages), 1), 5)
    nav_urls = {n["url"] for n in nav}
    known = site_data.known_urls(ctx.conn, ctx.site_id)
    url, found, listing_pages, stored = match["url"], [], 0, 0
    try:
        for _ in range(max_pages):
            page = ctx.fetch(url)
            listing_pages += 1
            if page.status != 200:
                break
            html = page.text()
            if extract.needs_browser(html) and not extract.links(html, page.url):  # the listing is built by JavaScript
                rendered = ctx.render(page.url)
                if rendered is not None:
                    page, html = rendered, rendered.text()
            embedded = extract.products_from_jsonld(html, page.url)
            if len(embedded) > 1:  # listing pages that carry their products' data save the product fetches
                for item in embedded:
                    ctx.record({**item, "category": item.get("category") or match["name"], "source_url": page.url})
                    stored += 1
            for link in extract.links(html, page.url):
                if link in nav_urls or link.rstrip("/") == ctx.origin or link == page.url or _NOT_PRODUCT.search(link):
                    continue
                if ctx.robots and not ctx.robots.allowed(link):
                    continue
                ctx.url_category.setdefault(link, match["name"])
                found.append(link)
            nxt = Selector(html).css('a[rel="next"]::attr(href), link[rel="next"]::attr(href)').get()
            if not nxt:
                break
            url = urljoin(page.url, nxt)
    except (ToolStop, SkipURL):
        pass
    found = list(dict.fromkeys(found))
    if ctx.sort == "newest" and ctx.url_lastmod:  # sitemap dates give the order; undated links keep listing order
        found = sorted(found, key=lambda u: -(ctx.url_lastmod[u].timestamp() if u in ctx.url_lastmod else 0))
    new = [u for u in found if u not in known and u not in ctx.products_seen]
    ctx.queues["all"] = deque(new + [u for u in ctx.queues["all"] if u not in set(new)])
    return "\n".join([
        f'"{match["name"]}": {listing_pages} listing page(s) | {len(found)} product links | {len(new)} not stored yet, queued'
        + (f" | {stored} products read from the listing itself" if stored else ""),
        ctx.status_line()])


def _fetch_products(ctx: RunContext, urls: deque, n: int, known_category: dict | None = None) -> Counter:
    """Fetch product pages from `urls` (consumed in place) and record what they carry."""
    counts = Counter()
    try:
        while urls and counts["fetched"] < n and not ctx.limit_hit:
            url = urls.popleft()
            if url in ctx.products_seen or url in ctx.fetched_urls:
                continue
            ctx.fetched_urls.add(url)
            try:
                page = ctx.fetch(url)
            except SkipURL:
                counts["failed"] += 1
                continue
            counts["fetched"] += 1
            if page.status in (404, 410):
                counts["not found"] += 1
                if row := site_data.note_missing(ctx.conn, ctx.site_id, url):
                    ctx.add_events([{"type": "product_removed", "category": row["category"], "before": cd._snap(row),
                                     "after": None, "occurred_at": ctx.started}], product=row)
                continue
            if page.status != 200:
                counts["failed"] += 1
                continue
            html, final_url = page.text(), page.url
            items = extract.extract_products(html, final_url)
            if not items and extract.needs_browser(html):
                try:
                    rendered = ctx.render(final_url)
                except SkipURL:
                    rendered = None
                if rendered is None:
                    counts["needs browser"] += 1
                    continue
                html, final_url = rendered.text(), rendered.url
                items = [{**i, "extracted_by": f"{i['extracted_by']}+browser"} for i in extract.extract_products(html, final_url)]
                counts["rendered"] += 1
            if not items and ctx.ai_extract and ctx.cfg.get("ai_extract") and ctx.ai_used < ctx.cfg.get("ai_extract_cap", 0) \
                    and not extract.needs_browser(html):
                ctx.ai_used += 1
                try:
                    item = ctx.ai_extract(html, final_url)
                except Exception as e:  # noqa: BLE001 - the AI is optional: note it and move on
                    ctx.trace.append({"node": "ai_extract", "error": f"{type(e).__name__}: {e}"[:200]})
                    item = None
                items = [item] if item else []
                counts["read by AI"] += bool(items)
            if not items:
                counts["no product data"] += 1
                continue
            for item in items:
                if not item.get("category") and known_category:
                    item = {**item, "category": known_category.get(url)}
                item = {**item, "source_url": final_url}
                if not item.get("source_date") and url in ctx.url_lastmod:
                    # ponytail: lastmod is "last modified", so on a first run a recently edited product counts as new
                    item = {**item, "source_date": ctx.url_lastmod[url]}
                ctx.record(item)
                counts["stored"] += 1
    except ToolStop:
        pass
    return counts


@tool
def fetch_queued_products(ctx: RunContext, source: str = "all", limit: int | None = None) -> str:
    if source not in ("recent", "all", "sample"):
        return f'ERROR: source must be "recent", "all" or "sample"\n{ctx.status_line()}'
    queue = ctx.queues[source]
    if not queue:
        return f'ERROR: queue "{source}" is empty; call read_sitemap or fetch_category_listing first\n{ctx.status_line()}'
    before = len(ctx.events)
    c = _fetch_products(ctx, queue, min(limit, ctx.page_budget) if limit else ctx.page_budget)
    extra = " | ".join(f"{k} {c[k]}" for k in ("rendered", "read by AI", "no product data", "needs browser", "not found",
                                                "failed") if c[k])
    return "\n".join([
        f"fetched {c['fetched']} | stored {c['stored']} | {tally(ctx.events[before:])}" + (f" | {extra}" if extra else ""),
        f"left in queue: {len(queue)}",
        ctx.status_line()])


@tool
def recheck_known_products(ctx: RunContext, limit: int | None = None) -> str:
    room = ctx.page_budget - ctx.pages_used
    n = min(limit, room) if limit else room
    rows = site_data.least_recently_checked(ctx.conn, ctx.site_id, max(n, 0), ctx.products_seen | ctx.fetched_urls)
    if not rows:
        return f"nothing to recheck: no stored products left to check\n{ctx.status_line()}"
    before = len(ctx.events)
    c = _fetch_products(ctx, deque(r["url"] for r in rows), n, {r["url"]: r["category"] for r in rows})
    evs = ctx.events[before:]
    t = Counter(e["type"] for e in evs)
    return "\n".join([
        (f"rechecked {c['fetched']} | price changes {t['price_drop'] + t['price_rise']} ({t['price_drop']} down) | "
         f"back in stock {t['back_in_stock']} | out of stock {t['out_of_stock']} | not found {c['not found']}"),
        ctx.status_line()])


def run_default_plan(ctx: RunContext) -> list[str]:
    """The fixed tool order used when the LLM is unavailable (agent spec §1.3, fallback_plan)."""
    out = [] if ctx.inspected else [inspect_site(ctx)]
    if ctx.stop_reason:
        return out
    if ctx.has_feed:
        out.append(fetch_product_feed(ctx))
        return out
    first = ctx.run_type == "first"
    out.append(read_sitemap(ctx, None if first else ctx.days_since_last_run))
    if ctx.categories:
        for c in ctx.categories:
            if not ctx.stop_reason:
                out.append(fetch_category_listing(ctx, c["name"], 5))
        if ctx.queues["all"] and not ctx.stop_reason:
            out.append(fetch_queued_products(ctx, "all"))
        if not first and not ctx.stop_reason and not ctx.limit_hit:
            out.append(recheck_known_products(ctx))
        return out
    if not ctx.queues["all"] and not ctx.queues["recent"] and not ctx.stop_reason:
        for n in ctx.nav[:3]:
            out.append(fetch_category_listing(ctx, n["name"]))
    for step in ([("queued", "all")] if first else [("queued", "recent"), ("recheck", None), ("queued", "sample")]):
        if ctx.stop_reason or ctx.limit_hit:
            break
        if step[0] == "recheck":
            out.append(recheck_known_products(ctx))
        elif ctx.queues[step[1]]:
            out.append(fetch_queued_products(ctx, step[1]))
    return out
