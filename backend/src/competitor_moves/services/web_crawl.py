"""The public crawl API's engine (/v1/web/scrape, /v1/web/crawl): 1 credit per page fetched.

Free checks (URL safety, robots.txt) run before a credit is taken; the credit is spent once the page is
requested, even if the site then errors.
"""
import time
import uuid
from collections import deque
from datetime import UTC, datetime
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

from ..config import get_settings
from ..core.ssrf import BlockedURL, check_url
from ..crawler.fetch import FetchError, Page
from ..crawler.fetch import fetch as http_fetch
from ..crawler.markdown import html_to_markdown
from ..crawler.robots import Robots
from ..db.repositories import credits
from ..workers import queue

TEXT_TYPES = ("text/html", "application/xhtml+xml", "text/plain", "application/xml", "text/xml")
MAX_MARKDOWN = 100_000  # per page, keeps a 50-page job result bounded
ROBOTS_TTL_SEC = 3600
_robots_cache: dict[str, tuple[float, Robots]] = {}  # ponytail: per-process and unbounded; add LRU if origins pile up


class ScrapeError(Exception):
    """The message is safe to return to the API caller."""


class CrawlRequest(BaseModel):
    url: str = Field(max_length=2000)
    limit: int = 10
    max_depth: int = 1
    include_paths: list[str] = Field(default_factory=list, max_length=20)
    exclude_paths: list[str] = Field(default_factory=list, max_length=20)
    allow_external_links: bool = False

    @field_validator("url")
    @classmethod
    def _http_url(cls, v: str) -> str:
        p = urlsplit(v)
        if p.scheme not in ("http", "https") or not p.hostname:
            raise ValueError("`url` must be an absolute http(s) URL")
        return v

    @field_validator("limit")
    @classmethod
    def _limit(cls, v: int) -> int:
        return min(max(v, 1), 50)

    @field_validator("max_depth")
    @classmethod
    def _depth(cls, v: int) -> int:
        return min(max(v, 0), 3)


def _fetch(url: str) -> Page:
    s = get_settings()
    return http_fetch(url, user_agent=s.user_agent, timeout=s.fetch_timeout_sec)


def _origin(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


def robots_for(url: str) -> Robots:
    origin = _origin(url)
    hit = _robots_cache.get(origin)
    if hit and time.monotonic() - hit[0] < ROBOTS_TTL_SEC:
        return hit[1]
    r = Robots.load(origin, _fetch, get_settings().user_agent)
    _robots_cache[origin] = (time.monotonic(), r)
    return r


def preflight(url: str) -> None:
    """Free checks before spending a credit. Raises ScrapeError."""
    try:
        check_url(url)
    except BlockedURL as e:
        raise ScrapeError(str(e)) from None
    if not robots_for(url).allowed(url):
        raise ScrapeError("blocked by the site's robots.txt")


def fetch_markdown(url: str) -> dict:
    try:
        page = _fetch(url)
    except BlockedURL as e:  # a redirect pointed somewhere private
        raise ScrapeError(str(e)) from None
    except FetchError as e:
        raise ScrapeError(f"fetch failed: {e}") from None
    if page.content_type and not any(t in page.content_type for t in TEXT_TYPES):
        raise ScrapeError(f"unsupported content-type {page.content_type.split(';')[0]}")
    out = html_to_markdown(page.text(), page.url)
    if len(out["markdown"]) > MAX_MARKDOWN:
        out["markdown"], out["markdown_truncated"] = out["markdown"][:MAX_MARKDOWN], True
    return {"url": page.url, "status": page.status, **out, "fetched_at": datetime.now(UTC).isoformat()}


def scrape(conn, user_id: int, url: str) -> dict:
    """One page. Raises ScrapeError or credits.InsufficientCredits."""
    preflight(url)
    credits.charge(conn, user_id, 1)
    return fetch_markdown(url)


def job_view(job: dict) -> dict:
    status = {"queued": "queued", "running": "running", "done": "completed", "failed": "failed"}[job["status"]]
    return {"job_id": job["public_id"], "kind": "crawl", "status": status, "progress": job["progress"],
            "result": job["result"], "error": job["error"] if status == "failed" else None,
            "created_at": job["created_at"].isoformat()}


def start_crawl(conn, user_id: int, req: CrawlRequest) -> dict:
    try:
        check_url(req.url)  # fail fast on obviously bad targets; every page is checked again when fetched
    except BlockedURL as e:
        raise ScrapeError(str(e)) from None
    job = queue.enqueue(conn, "web_crawl", req.model_dump(), user_id=user_id,
                        public_id="crawl_" + uuid.uuid4().hex[:12], progress={"completed": 0, "total": req.limit})
    return job_view(job)


def get_job(conn, user_id: int, public_id: str) -> dict | None:
    job = conn.execute("select * from jobs where public_id=%s and user_id=%s and type='web_crawl'",
                       (public_id, user_id)).fetchone()
    return job_view(job) if job else None


def _path_ok(path: str, req: CrawlRequest) -> bool:
    if req.include_paths and not any(path.startswith(p) for p in req.include_paths):
        return False
    return not any(path.startswith(p) for p in req.exclude_paths)


def run_job(conn, job: dict) -> dict:
    """Worker handler: breadth-first crawl from req.url, same origin unless allow_external_links."""
    req = CrawlRequest(**job["payload"])
    root = _origin(req.url)
    base_delay = get_settings().crawl_delay_sec
    todo, seen, pages = deque([(req.url, 0)]), {req.url.split("#")[0]}, []
    last_fetch: dict[str, float] = {}
    while todo and len(pages) < req.limit:
        url, depth = todo.popleft()
        try:
            preflight(url)
            delay = max(base_delay, min(robots_for(url).crawl_delay or 0, 10))
            wait = last_fetch.get(_origin(url), 0) + delay - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            credits.charge(conn, user_id=job["user_id"], n=1)
            last_fetch[_origin(url)] = time.monotonic()
            page = fetch_markdown(url)
        except credits.InsufficientCredits:
            raise queue.JobFailed("insufficient credits", {"pages": pages}) from None
        except ScrapeError as e:
            pages.append({"url": url, "error": str(e), "depth": depth})
            continue
        pages.append({**page, "depth": depth})
        queue.set_progress(conn, job["id"], {"completed": len(pages), "total": req.limit})
        if depth >= req.max_depth:
            continue
        for link in page["links"]:
            link = link.split("#")[0]
            if link in seen:
                continue
            internal = _origin(link) == root
            if (not internal and not req.allow_external_links) or (internal and not _path_ok(urlsplit(link).path, req)):
                continue
            seen.add(link)
            try:
                preflight(link)  # blocked links are skipped like external ones; only the start URL reports errors
            except ScrapeError:
                continue
            todo.append((link, depth + 1))
    queue.set_progress(conn, job["id"], {"completed": len(pages), "total": len(pages)})
    return {"pages": pages}
