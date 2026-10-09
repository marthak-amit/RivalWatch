"""Postgres job queue: jobs are claimed with FOR UPDATE SKIP LOCKED, so several workers can run safely."""
from psycopg.types.json import Jsonb

MAX_ATTEMPTS = {"web_crawl": 1}  # user-facing and already charged per page: never retried automatically
DEFAULT_MAX_ATTEMPTS = 3


class JobFailed(Exception):
    """Raised by a handler to fail a job while keeping a partial result."""

    def __init__(self, error: str, result: dict | None = None):
        super().__init__(error)
        self.result = result


def enqueue(conn, job_type: str, payload: dict, *, user_id: int | None = None, public_id: str | None = None,
            progress: dict | None = None) -> dict:
    return conn.execute(
        """insert into jobs(type, payload, user_id, public_id, progress) values (%s,%s,%s,%s,%s) returning *""",
        (job_type, Jsonb(payload), user_id, public_id, Jsonb(progress) if progress is not None else None)).fetchone()


def claim(conn) -> dict | None:
    return conn.execute(
        """update jobs set status='running', attempts=attempts+1, started_at=now()
           where id = (select id from jobs where status='queued' and run_after <= now()
                       order by id for update skip locked limit 1)
           returning *""").fetchone()


def set_progress(conn, job_id: int, progress: dict) -> None:
    conn.execute("update jobs set progress=%s where id=%s", (Jsonb(progress), job_id))


def complete(conn, job_id: int, result: dict | None) -> None:
    conn.execute("update jobs set status='done', result=%s, error=null, finished_at=now() where id=%s",
                 (Jsonb(result) if result is not None else None, job_id))


def fail(conn, job: dict, error: str, result: dict | None = None, *, final: bool = False) -> None:
    """Re-queue with backoff (30s, 2min, ...) until the type's attempt limit (or now if final), then mark failed."""
    if not final and job["attempts"] < MAX_ATTEMPTS.get(job["type"], DEFAULT_MAX_ATTEMPTS):
        conn.execute("""update jobs set status='queued', error=%s,
                        run_after = now() + make_interval(secs => 30 * power(4, attempts - 1)) where id=%s""",
                     (error, job["id"]))
    else:
        conn.execute("update jobs set status='failed', error=%s, result=%s, finished_at=now() where id=%s",
                     (error, Jsonb(result) if result is not None else None, job["id"]))


def recover_stale(conn, older_than_min: int = 60) -> int:
    """Jobs left 'running' by a worker that died. ponytail: age-based, assumes no job legitimately runs >1h."""
    return conn.execute(
        """update jobs set status='failed', error='worker stopped while running', finished_at=now()
           where status='running' and started_at < now() - make_interval(mins => %s) returning id""",
        (older_than_min,)).rowcount
