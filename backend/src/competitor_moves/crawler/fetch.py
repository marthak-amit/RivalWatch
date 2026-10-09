"""HTTP GET through Scrapling's fetcher, following redirects ourselves so every hop passes the SSRF guard."""
import logging
from dataclasses import dataclass
from urllib.parse import urljoin

from curl_cffi.requests.exceptions import RequestException
from scrapling.fetchers import Fetcher

from ..core.ssrf import check_url

logging.getLogger("scrapling").setLevel(logging.CRITICAL)  # it logs every fetch at INFO and failures at ERROR

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


def fetch(url: str, *, user_agent: str, timeout: float = 20, retries: int = 2, max_redirects: int = 5,
          max_bytes: int = 5_000_000, accept: str = ACCEPT_HTML) -> Page:
    """Raises BlockedURL (SSRF) or FetchError (network, too many redirects). HTTP error codes are returned."""
    current = url
    for _ in range(max_redirects + 1):
        check_url(current)
        try:
            r = Fetcher.get(current, follow_redirects=False, timeout=timeout, retries=retries,
                            stealthy_headers=False, headers={"User-Agent": user_agent, "Accept": accept})
        except RequestException as e:
            raise FetchError(str(e).split(". See ")[0][:200]) from None
        headers = {k.lower(): v for k, v in r.headers.items()}
        location = headers.get("location")
        if 300 <= r.status < 400 and location:
            current = urljoin(current, location)
            continue
        # ponytail: the whole body is downloaded before truncation; stream it if huge pages become a problem
        return Page(url=current, status=r.status, headers=headers, body=bytes(r.body[:max_bytes]))
    raise FetchError("too many redirects")
