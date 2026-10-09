"""Worker process: runs queued jobs one at a time.  python -m competitor_moves.workers.worker"""
import logging
import signal
import time

from ..agents.compare import graph as compare_graph
from ..agents.crawl import graph as crawl_graph
from ..db.pool import close_pool, get_pool
from ..services import digests, store_sync, web_crawl
from . import queue

log = logging.getLogger("worker")
HANDLERS = {"web_crawl": web_crawl.run_job, "crawl_site": crawl_graph.run_job, "sync_store": store_sync.run_job,
            "compare_project": compare_graph.run_job}


def run_once(conn) -> bool:
    """Run one job if one is waiting. Returns False when the queue is empty."""
    job = queue.claim(conn)
    if job is None:
        return False
    handler = HANDLERS.get(job["type"])
    try:
        if handler is None:
            raise queue.JobFailed(f"unknown job type {job['type']}")
        queue.complete(conn, job["id"], handler(conn, job))
        if job["type"] in digests.CRAWL_JOBS:  # the last crawl/sync of a round queues the workspace's digest
            digests.maybe_queue(conn, int(job["payload"]["site_id"]))
    except queue.JobFailed as e:
        queue.fail(conn, job, str(e), e.result, final=True)  # the handler decided: no retry
    except Exception as e:  # a bug or outage in one job must not kill the worker
        log.exception("job %s (%s) failed", job["id"], job["type"])
        queue.fail(conn, job, f"{type(e).__name__}: {e}"[:500])
    return True


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True
        log.info("stopping after the current job")

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with get_pool().connection() as conn:
        if n := queue.recover_stale(conn):
            log.warning("marked %s stale job(s) failed", n)
    while not stopping:
        with get_pool().connection() as conn:
            busy = run_once(conn)
        if not busy:
            time.sleep(1)
    close_pool()


if __name__ == "__main__":
    main()
