"""robots.txt rules for one origin (RFC 9309: 4xx = no rules, 5xx or unreachable = disallow everything)."""
from collections.abc import Callable
from urllib.robotparser import RobotFileParser

from ..core.ssrf import BlockedURL
from .fetch import FetchError, Page


class Robots:
    def __init__(self, parser: RobotFileParser, user_agent: str):
        self._rp = parser
        self._ua = user_agent

    @classmethod
    def load(cls, origin: str, fetch: Callable[[str], Page], user_agent: str) -> "Robots":
        rp = RobotFileParser()
        try:
            page = fetch(origin.rstrip("/") + "/robots.txt")
        except (FetchError, BlockedURL):
            rp.disallow_all = True
        else:
            if page.status >= 500:
                rp.disallow_all = True
            elif page.status >= 400:
                rp.allow_all = True
            else:
                rp.parse(page.text().splitlines())
        return cls(rp, user_agent)

    @classmethod
    def allow_everything(cls, user_agent: str = "*") -> "Robots":
        rp = RobotFileParser()
        rp.allow_all = True
        return cls(rp, user_agent)

    def allowed(self, url: str) -> bool:
        return self._rp.can_fetch(self._ua, url)

    @property
    def sitemaps(self) -> list[str]:
        return list(self._rp.site_maps() or [])

    @property
    def crawl_delay(self) -> float | None:
        d = self._rp.crawl_delay(self._ua)
        return float(d) if d is not None else None
