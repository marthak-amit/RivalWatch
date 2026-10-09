"""Reads and writes inside one site's own schema (site_<id>). Product writes live in services/change_detection."""
from datetime import datetime

from psycopg import sql
from psycopg.types.json import Jsonb

from ..site_schema import table


def product_count(conn, site_id: int) -> int:
    return conn.execute(sql.SQL("select count(*) as n from {} where removed_at is null").format(
        table(site_id, "products"))).fetchone()["n"]


def known_urls(conn, site_id: int) -> set[str]:
    return {r["url"] for r in conn.execute(sql.SQL("select url from {}").format(table(site_id, "products"))).fetchall()}


def least_recently_checked(conn, site_id: int, limit: int, exclude: set[str]) -> list[dict]:
    return conn.execute(sql.SQL(
        "select id, url, category from {} where removed_at is null and not (url = any(%s)) "
        "order by last_checked_at nulls first, id limit %s").format(table(site_id, "products")),
        (list(exclude), limit)).fetchall()


def categories(conn, site_id: int) -> list[dict]:
    """Category, product count and price range of current products (used for 'why it matters')."""
    return conn.execute(sql.SQL(
        "select category, count(*) as products, round(min(price), 2) as min_price, round(max(price), 2) as max_price "
        "from {} where removed_at is null and category is not null group by category order by count(*) desc limit 40"
    ).format(table(site_id, "products"))).fetchall()


def last_run(conn, site_id: int) -> dict | None:
    return conn.execute(sql.SQL(
        "select * from {} where status in ('ok','partial') and finished_at is not null order by id desc limit 1"
    ).format(table(site_id, "crawl_runs"))).fetchone()


def start_run(conn, site_id: int, run_type: str) -> int:
    return conn.execute(sql.SQL("insert into {} (run_type) values (%s) returning id").format(
        table(site_id, "crawl_runs")), (run_type,)).fetchone()["id"]


def finish_run(conn, site_id: int, run_id: int, **fields) -> None:
    fields = {k: Jsonb(v) if isinstance(v, (dict, list)) else v for k, v in fields.items()}
    sets = sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(k)) for k in fields)
    conn.execute(sql.SQL("update {} set {}, finished_at = now() where id = %s").format(
        table(site_id, "crawl_runs"), sets), [*fields.values(), run_id])


def window_events(conn, site_id: int, since: datetime) -> list[dict]:
    return conn.execute(sql.SQL(
        "select e.id, e.type, e.product_id, e.category, e.before, e.after, e.occurred_at, p.title, p.url "
        "from {} e left join {} p on p.id = e.product_id where e.occurred_at >= %s order by e.occurred_at, e.id"
    ).format(table(site_id, "history"), table(site_id, "products")), (since,)).fetchall()


def recent_event_exists(conn, site_id: int, kind: str, category: str, since: datetime) -> bool:
    return conn.execute(sql.SQL(
        "select 1 from {} where type=%s and category=%s and occurred_at >= %s limit 1").format(
        table(site_id, "history")), (kind, category, since)).fetchone() is not None


def landing_pages(conn, site_id: int) -> set[str]:
    return {r["url"] for r in conn.execute(sql.SQL("select url from {} where kind='landing'").format(
        table(site_id, "pages"))).fetchall()}


def save_landing_pages(conn, site_id: int, urls: set[str], *, complete: bool) -> None:
    pages = table(site_id, "pages")
    with conn.transaction():
        if complete:
            conn.execute(sql.SQL("delete from {} where kind='landing' and not (url = any(%s))").format(pages), (list(urls),))
        for u in urls:
            conn.execute(sql.SQL(
                "insert into {} (url, kind, content_hash) values (%s, 'landing', '') "
                "on conflict (url) do update set last_fetched_at = now()").format(pages), (u,))


def note_missing(conn, site_id: int, url: str) -> dict | None:
    """A known product URL answered 404/410. Returns the product row once it has been missing twice in a row."""
    row = conn.execute(sql.SQL(
        "update {} set missing_count = missing_count + 1, last_checked_at = now() where url=%s and removed_at is null "
        "returning id, url, title, category, price, currency, source_url, missing_count").format(table(site_id, "products")),
        (url,)).fetchone()
    if row and row["missing_count"] >= 2:
        conn.execute(sql.SQL("update {} set removed_at = now() where id=%s").format(table(site_id, "products")), (row["id"],))
        return row
    return None


def urls_needing_meta(conn, site_id: int, urls: list[str], older_than: datetime) -> list[str]:
    rows = conn.execute(sql.SQL(
        "select url from {} where url = any(%s) and (meta_fetched_at is null or meta_fetched_at < %s)").format(
        table(site_id, "products")), (urls, older_than)).fetchall()
    found = {r["url"] for r in rows}
    return [u for u in urls if u in found]


def progress(conn, site_id: int, run_id: int, pages: int, products: int) -> None:
    conn.execute(sql.SQL("update {} set pages_fetched=%s, products_seen=%s where id=%s").format(table(site_id, "crawl_runs")),
                 (pages, products, run_id))


def save_home(conn, site_id: int, run_id: int, home: dict) -> None:
    """Store the homepage reading as soon as it is taken, so the dashboard shows it while the crawl continues."""
    conn.execute(sql.SQL("update {} set home=%s where id=%s").format(table(site_id, "crawl_runs")), (Jsonb(home), run_id))
