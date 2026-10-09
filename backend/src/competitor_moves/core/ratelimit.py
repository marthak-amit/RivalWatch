"""Login attempt limits, shared by every login route (/user/login, /admin/login, /api/auth/login).

Failed logins count per IP+email (10 per 5 minutes); a successful login clears the count. Signups count every
attempt per IP. ponytail: in-process memory, so per API worker; move to Postgres/Redis if the API runs several.
"""
import time
from collections import defaultdict

MAX_ATTEMPTS, WINDOW_SEC = 10, 300


class Limiter:
    def __init__(self, max_hits: int = MAX_ATTEMPTS, window_sec: int = WINDOW_SEC):
        self.max, self.window, self.hits = max_hits, window_sec, defaultdict(list)

    def _recent(self, key: str) -> list[float]:
        now = time.monotonic()
        self.hits[key] = [t for t in self.hits[key] if now - t < self.window]
        return self.hits[key]

    def blocked(self, key: str) -> bool:
        return len(self._recent(key)) >= self.max

    def fail(self, key: str) -> None:
        self._recent(key).append(time.monotonic())

    def clear(self, key: str) -> None:
        self.hits.pop(key, None)


logins = Limiter()
signups = Limiter()


def login_key(ip: str | None, email: str) -> str:
    return f"{ip or '?'}|{email.strip().lower()}"
