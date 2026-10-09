"""Scheduler process: every minute, queue a crawl for each active competitor site whose cron time has come.
    python -m competitor_moves.workers.scheduler

Cron expressions are evaluated in UTC. A site with an invalid expression falls back to the daily default.
"""
import logging
import signal
import time
from datetime import UTC, datetime

from croniter import croniter
from psycopg.types.json import Jsonb

from ..config import DEFAULT_CRON
from ..db.pool import close_pool, get_pool

log = logging.getLogger("scheduler")
TICK_SEC = 60


def next_run(cron: str, after: datetime) -> datetime:
    try:
        return croniter(cron, after).get_next(datetime)
    except (ValueError, KeyError):
        return croniter(DEFAULT_CRON, after).get_next(datetime)


def tick(conn, now: datetime | None = None) -> int:
    """Queue due crawls and move each site's next_run_at forward. Returns how many jobs were queued."""
    now = now or datetime.now(UTC)
    due = conn.execute("""select s.id, s.cron from sites s join projects p on p.id = s.project_id
                          left join users u on u.id = p.owner_user_id
                          where s.status='active' and s.role='competitor' and (u.id is null or u.is_active)
                          and (s.next_run_at is null or s.next_run_at <= %s) order by s.id""", (now,)).fetchall()
    queued = 0
    for site in due:
        with conn.transaction():
            # the unique index on active jobs means a site already queued or running is not queued twice
            row = conn.execute("insert into jobs(type, payload) values ('crawl_site', %s) on conflict do nothing returning id",
                               (Jsonb({"site_id": site["id"]}),)).fetchone()
            conn.execute("update sites set next_run_at=%s where id=%s", (next_run(site["cron"], now), site["id"]))
        queued += row is not None
    return queued


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopping:
        try:
            with get_pool().connection() as conn:
                if n := tick(conn):
                    log.info("queued %s crawl(s)", n)
        except Exception:
            log.exception("scheduler tick failed")
        for _ in range(TICK_SEC):
            if stopping:
                break
            time.sleep(1)
    close_pool()


if __name__ == "__main__":
    main()
