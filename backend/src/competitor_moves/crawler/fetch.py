"""HTTP GET with curl_cffi (the client Scrapling's fetcher wraps: a real browser's TLS fingerprint), following redirects
ourselves so every hop passes the SSRF guard. Parsing stays with Scrapling's Selector.

Scrapling's own Fetcher isn't used because importing it loads its browser stacks (patchright, playwright,
browserforge: ~300 MB) into every process and image, even for plain HTTP.
"""
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from curl_cffi import requests as curl
from curl_cffi.requests.exceptions import RequestException

from ..core.ssrf import check_url

IMPERSONATE = "chrome"  # the newest Chrome profile curl_cffi ships

ACCEPT_HTML = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"


class FetchError(Exception):
    pass


@dataclass
class Page:
    url: str  # final URL after redirects
    status: int
    headers: dict[str, str]  # lower-cased names
    body: bytes

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").lower()

    def text(self) -> str:
        charset = "utf-8"
        for part in self.content_type.split(";")[1:]:
            k, _, v = part.strip().partition("=")
            if k == "charset" and v:
                charset = v.strip('"\' ')
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").removeprefix("www.")


def fetch(url: str, *, user_agent: str, timeout: float = 20, retries: int = 2, max_redirects: int = 5,
          max_bytes: int = 5_000_000, accept: str = ACCEPT_HTML, stay_on: str | None = None,
          headers: dict[str, str] | None = None) -> Page:
    """Raises BlockedURL (SSRF) or FetchError (network, too many redirects, or a redirect off `stay_on`'s site).
    HTTP error codes are returned, not raised."""
    current = url
    for _ in range(max_redirects + 1):
        check_url(current)
        r = _get(current, timeout, retries, {"User-Agent": user_agent, "Accept": accept, **(headers or {})})
        resp_headers = {k.lower(): v for k, v in r.headers.items()}
        location = resp_headers.get("location")
        if 300 <= r.status_code < 400 and location:
            current = urljoin(current, location)
            if stay_on and _host(current) != _host(stay_on):
                raise FetchError(f"redirected off-site to {current}")
            continue
        # ponytail: the whole body is downloaded before truncation; stream it if huge pages become a problem
        return Page(url=current, status=r.status_code, headers=resp_headers, body=bytes(r.content[:max_bytes]))
    raise FetchError("too many redirects")


def send(url: str, body, *, user_agent: str, timeout: float = 30, headers: dict[str, str] | None = None) -> Page:
    """One JSON POST to an API: SSRF-checked, never redirected and never retried (a write must not run twice)."""
    check_url(url)
    try:
        r = curl.post(url, json=body, timeout=timeout, allow_redirects=False, impersonate=IMPERSONATE,
                      headers={"User-Agent": user_agent, "Accept": "application/json", "Content-Type": "application/json",
                               **(headers or {})})
    except RequestException as e:
        raise FetchError(str(e).split(". See ")[0][:200]) from None
    return Page(url=url, status=r.status_code, headers={k.lower(): v for k, v in r.headers.items()},
                body=bytes(r.content[:1_000_000]))


def _get(url: str, timeout: float, retries: int, headers: dict[str, str]):
    """One request (no redirect following), retried on network errors with a short pause."""
    for attempt in range(retries + 1):
        try:
            return curl.get(url, headers=headers, timeout=timeout, allow_redirects=False, impersonate=IMPERSONATE)
        except RequestException as e:
            if attempt == retries:
                raise FetchError(str(e).split(". See ")[0][:200]) from None
            time.sleep(min(1.0 * (attempt + 1), 3))
