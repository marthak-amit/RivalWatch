from datetime import UTC, datetime

from tests.agents.test_crawl_graph import CATALOG, shopify

from competitor_moves.workers import scheduler, worker

NOON = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def jobs_for(conn, sid):
    return conn.execute("select status from jobs where type='crawl_site' and payload->>'site_id'=%s", (str(sid),)).fetchall()


def next_run(conn, sid):
    return conn.execute("select next_run_at from sites where id=%s", (sid,)).fetchone()["next_run_at"]


def test_due_site_is_queued_once_and_its_next_run_moves_forward(make_site, conn, site):
    sid = make_site(site.base + "/")
    scheduler.tick(conn, NOON)
    assert len(jobs_for(conn, sid)) == 1 and next_run(conn, sid) == datetime(2026, 10, 10, 2, 0, tzinfo=UTC)
    scheduler.tick(conn, NOON)
    assert len(jobs_for(conn, sid)) == 1  # not due yet
    conn.execute("update sites set next_run_at = %s where id=%s", (NOON, sid))
    scheduler.tick(conn, NOON)
    assert len(jobs_for(conn, sid)) == 1  # due again, but a crawl is still queued: no duplicate
    assert next_run(conn, sid) == datetime(2026, 10, 10, 2, 0, tzinfo=UTC)


def test_custom_and_invalid_cron_and_blocked_sites(make_site, conn, site):
    hourly = make_site(site.base + "/a", cron="15 * * * *")
    broken = make_site(site.base + "/b", cron="every day please")
    blocked = make_site(site.base + "/c", status="blocked")
    scheduler.tick(conn, NOON)
    assert next_run(conn, hourly) == datetime(2026, 10, 9, 12, 15, tzinfo=UTC)
    assert next_run(conn, broken) == datetime(2026, 10, 10, 2, 0, tzinfo=UTC)  # falls back to the daily default
    assert jobs_for(conn, blocked) == [] and next_run(conn, blocked) is None


def test_worker_runs_a_scheduled_crawl(make_site, conn, site):
    shopify(site, CATALOG)
    sid = make_site(site.base + "/")
    scheduler.tick(conn, NOON)
    while worker.run_once(conn):
        pass
    assert [j["status"] for j in jobs_for(conn, sid)] == ["done"]
    run = conn.execute(f"select status, products_seen from site_{sid}.crawl_runs").fetchone()
    assert run == {"status": "ok", "products_seen": 3}
