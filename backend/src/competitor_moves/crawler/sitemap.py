"""Sitemap reading: index and urlset files, gzip, lastmod. Child sitemaps older than `since` are skipped unread."""
import gzip
import io
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from xml.etree import ElementTree as ET

from ..core.ssrf import BlockedURL
from .fetch import FetchError, Page

MAX_SITEMAP_BYTES = 50 * 1024 * 1024  # the sitemap protocol's own limit (uncompressed)
URLS_PER_FILE = 50_000                # protocol limit; used to estimate unread files


@dataclass
class Entry:
    loc: str
    lastmod: datetime | None


@dataclass
class Sitemap:
    is_index: bool
    entries: list[Entry]


@dataclass
class Scan:
    urls: list[Entry] = field(default_factory=list)
    files_read: int = 0
    files_skipped: int = 0  # child sitemaps not read because their lastmod is older than `since`
    files_failed: int = 0
    files_unread: int = 0   # left in the queue when max_files or max_urls was reached
    index_files: int = 0
    truncated: bool = False

    @property
    def est_total(self) -> int:
        """URLs found plus an estimate for unread files (average URLs per file read so far)."""
        urlset_files = max(1, self.files_read - self.index_files)
        per_file = min(URLS_PER_FILE, max(1, len(self.urls) // urlset_files))
        return len(self.urls) + self.files_unread * per_file


def parse_lastmod(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        d = datetime.fromisoformat(text.strip())
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _ungzip(body: bytes) -> bytes:
    if body[:2] != b"\x1f\x8b":
        return body
    with gzip.GzipFile(fileobj=io.BytesIO(body)) as f:
        data = f.read(MAX_SITEMAP_BYTES + 1)  # bounded: protects against gzip bombs
    if len(data) > MAX_SITEMAP_BYTES:
        raise ValueError("sitemap larger than 50MB")
    return data


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse(body: bytes) -> Sitemap:
    """Raises ValueError on malformed or oversized input."""
    try:
        root = ET.fromstring(_ungzip(body))
    except (ET.ParseError, OSError, EOFError) as e:
        raise ValueError(f"not a sitemap: {e}") from None
    kind = _local(root.tag)
    if kind not in ("sitemapindex", "urlset"):
        raise ValueError(f"not a sitemap: <{kind}>")
    entries = []
    for el in root:
        loc = lastmod = None
        for child in el:
            name = _local(child.tag)
            if name == "loc":
                loc = (child.text or "").strip()
            elif name == "lastmod":
                lastmod = parse_lastmod(child.text)
        if loc:
            entries.append(Entry(loc, lastmod))
    return Sitemap(kind == "sitemapindex", entries)


def collect(fetch: Callable[[str], Page], roots: list[str], *, since: datetime | None = None,
            max_files: int = 50, max_urls: int = 200_000,
            keep_file: Callable[[str], bool] = lambda url: True) -> Scan:
    """Breadth-first over sitemap indexes. Returns every page URL with its lastmod (callers filter by date)."""
    scan = Scan()
    queue, seen = deque(roots), set()
    while queue:
        loc = queue.popleft()
        if loc in seen:
            continue
        seen.add(loc)
        if scan.files_read >= max_files or len(scan.urls) >= max_urls:
            scan.files_unread = len(queue) + 1
            scan.truncated = True
            break
        if not keep_file(loc):
            continue
        try:
            page = fetch(loc)
            if page.status != 200:
                raise ValueError(f"HTTP {page.status}")
            sm = parse(page.body)
        except (FetchError, BlockedURL, ValueError):
            scan.files_failed += 1
            continue
        scan.files_read += 1
        if sm.is_index:
            scan.index_files += 1
            for e in sm.entries:
                if since and e.lastmod and e.lastmod < since:
                    scan.files_skipped += 1
                else:
                    queue.append(e.loc)
        else:
            room = max_urls - len(scan.urls)
            scan.urls.extend(sm.entries[:room])
            if len(sm.entries) > room:
                scan.truncated = True
    return scan
