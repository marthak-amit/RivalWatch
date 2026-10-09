"""Try the crawler on a real website and see exactly what it collects.

Two modes:

  Whole site (the real crawl agent, writes to the dev database, cleans up afterwards unless --keep):
    python scripts/try_crawl.py https://www.example-jewellery.com/
    python scripts/try_crawl.py https://www.example-jewellery.com/ --max-products 30 --twice --keep
    python scripts/try_crawl.py https://www.example-jewellery.com/ --no-ai      # fixed plan, no Gemini

  One page only (just Scrapling fetch + extraction, no database, no AI):
    python scripts/try_crawl.py --page https://www.example-jewellery.com/products/some-ring
    python scripts/try_crawl.py --page https://www.example-jewellery.com/ --markdown

Needs the dev database for whole-site mode:  docker --context default compose up -d db  (then alembic upgrade head).
Robots.txt is respected: a site that disallows bots is reported as blocked, not crawled.
"""
import argparse
import os
import sys
import time
from collections import Counter
from urllib.parse import urlsplit

os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/competitor")

from competitor_moves.config import get_settings


def money(v) -> str:
    return "-" if v is None else f"{float(v):,.2f}"


def short_attrs(a: dict | None) -> str:
    return ", ".join(f"{k}={v}" for k, v in (a or {}).items() if v) or "-"


def table(rows: list[list], headers: list[str], widths: list[int]) -> None:
    def fmt(cells):
        return "  ".join(str(c if c is not None else "-")[:w].ljust(w) for c, w in zip(cells, widths, strict=True))
    print("  " + fmt(headers))
    print("  " + "  ".join("-" * w for w in widths))
    for r in rows:
        print("  " + fmt(r))


# ---- one page -------------------------------------------------------------------------------------------------

def try_page(url: str, show_markdown: bool) -> int:
    from competitor_moves.core.ssrf import BlockedURL
    from competitor_moves.crawler import extract, platforms
    from competitor_moves.crawler.fetch import FetchError, fetch
    from competitor_moves.crawler.markdown import html_to_markdown
    from competitor_moves.crawler.robots import Robots

    s = get_settings()
    p = urlsplit(url)
    origin = f"{p.scheme}://{p.netloc}"

    def get(u, accept="text/html,*/*;q=0.8"):
        return fetch(u, user_agent=s.user_agent, timeout=s.fetch_timeout_sec, accept=accept)

    print(f"\n== robots.txt for {origin}")
    robots = Robots.load(origin, get, s.user_agent)
    print(f"  allowed for {url}: {robots.allowed(url)} | sitemaps: {robots.sitemaps or 'none listed'}")
    if not robots.allowed(url):
        print("  -> this page is off-limits to bots; nothing fetched.")
        return 1
    t = time.monotonic()
    try:
        page = get(url)
    except (FetchError, BlockedURL) as e:
        print(f"  fetch failed: {e}")
        return 1
    html = page.text()
    print(f"\n== fetched (curl_cffi, parsed with Scrapling) in {time.monotonic() - t:.1f}s")
    print(f"  status {page.status} | final URL {page.url} | {page.content_type or '?'} | {len(page.body):,} bytes")
    meta = extract.page_meta(html, page.url)
    print(f"  platform: {platforms.detect(html)} | title: {meta['title']} | h1: {meta['h1']}")
    print(f"  menu ({len(meta['nav'])}): " + ", ".join(n["name"] for n in meta["nav"][:15]))
    print(f"  promotions: {meta['promotions'] or 'none found'}")
    types = Counter(t for n in extract._walk(extract.jsonld_nodes(html)) for t in extract._types(n))
    print(f"  JSON-LD types on the page: {dict(types) or 'none'}")
    products = extract.extract_products(html, page.url)
    print(f"\n== products found on this page: {len(products)}")
    if products:
        table([[x["title"], money(x["price"]), money(x["compare_at_price"]), x["currency"], x["in_stock"], x["sku"],
                x["category"], x["extracted_by"], short_attrs(x["attributes"])] for x in products[:20]],
              ["title", "price", "was", "cur", "in stock", "sku", "category", "source", "jewellery attributes"],
              [40, 10, 10, 4, 8, 14, 16, 6, 50])
    else:
        print("  (no product data on this page: normal for home/category pages without JSON-LD)")
    if show_markdown:
        md = html_to_markdown(html, page.url)["markdown"].splitlines()
        print(f"\n== page as markdown (first 40 of {len(md)} lines)")
        print("\n".join("  " + line for line in md[:40]))
    return 0


# ---- whole site -----------------------------------------------------------------------------------------------

def try_site(url: str, max_products: int, page_budget: int, use_ai: bool, twice: bool, keep: bool) -> int:
    import psycopg
    from psycopg import sql

    from competitor_moves.agents.crawl.graph import run_site_crawl
    from competitor_moves.db.pool import close_pool, get_pool
    from competitor_moves.db.site_schema import create_site_schema, drop_site_schema
    from competitor_moves.db.site_schema import table as site_table

    if use_ai and not get_settings().gemini_api_key:
        print("GEMINI_API_KEY is not set in backend/.env, so the fixed plan runs instead of the AI agent.\n")
    try:
        conn_ctx = get_pool().connection()
        conn = conn_ctx.__enter__()
    except psycopg.OperationalError as e:
        print(f"Can't reach the database ({e}).\nStart it with:  docker --context default compose up -d db")
        return 2
    domain = urlsplit(url).netloc
    pid = conn.execute("insert into projects(name,url) values ('try_crawl',%s) returning id", (url,)).fetchone()["id"]
    sid = conn.execute("insert into sites(project_id,domain,url,role,schema_name,max_products,page_budget) "
                       "values (%s,%s,%s,'competitor',md5(random()::text),%s,%s) returning id",
                       (pid, domain, url, max_products, page_budget)).fetchone()["id"]
    conn.execute("update sites set schema_name=%s where id=%s", (f"site_{sid}", sid))
    create_site_schema(conn, sid)
    llm_args = {} if use_ai else {"researcher_llm": None, "summarize_llm": None}
    try:
        for n in range(1, 3 if twice else 2):
            print(f"\n{'=' * 100}\n RUN {n} on {url}  (limit {max_products} products, {page_budget} pages)\n{'=' * 100}")
            t = time.monotonic()
            r = run_site_crawl(conn, sid, **llm_args)
            run = conn.execute(sql.SQL("select * from {} order by id desc limit 1").format(site_table(sid, "crawl_runs"))).fetchone()
            print(f"\n== result: {r['status'].upper()} in {time.monotonic() - t:.0f}s | pages fetched {run['pages_fetched']} "
                  f"({run['pages_failed']} failed) | products {run['products_seen']} | est. catalog size {run['est_total_products'] or '?'}"
                  f" | stop: {run['stop_reason'] or '-'} | models: {run['models']}")
            print("\n== what the agent did (each tool call; pages fetched with curl_cffi and parsed with Scrapling)")
            for step in run["trace"]:
                if "tool" in step:
                    args = f"({', '.join(map(str, step['args']))})" if step["args"] else "()"
                    print(f"  -> {step['tool']}{args}  [{step['pages']} pages, {step['seconds']}s]\n     {step['result']}")
                elif step.get("final"):
                    print(f"  AI finished: {step['final']}")
                elif step.get("error"):
                    print(f"  {step.get('node')}: {step['error']}")
                elif step.get("node") == "fallback_plan":
                    print("  (fixed plan used instead of the AI agent)")
            products = conn.execute(sql.SQL(
                "select title, price, compare_at_price, currency, in_stock, category, extracted_by, attributes, url "
                "from {} order by last_seen_at desc, id limit 15").format(site_table(sid, "products"))).fetchall()
            print(f"\n== products stored (showing {len(products)})")
            table([[x["title"], money(x["price"]), money(x["compare_at_price"]), x["in_stock"], x["category"], x["extracted_by"],
                    short_attrs(x["attributes"]), urlsplit(x["url"]).path] for x in products],
                  ["title", "price", "was", "in stock", "category", "source", "jewellery attributes", "path"],
                  [38, 10, 10, 8, 16, 6, 44, 30])
            hist = conn.execute(sql.SQL("select type, category, before, after from {} where run_id=%s order by id").format(
                site_table(sid, "history")), (run["id"],)).fetchall()
            print(f"\n== changes logged in history this run: {dict(Counter(h['type'] for h in hist)) or 'none'}")
            for h in hist[:8]:
                print(f"  {h['type']:<15} {h['category'] or ''!s:<16} before={h['before']}  after={h['after']}")
            moves = conn.execute("select type, category, headline, why_it_matters from moves where site_id=%s order by id",
                                 (sid,)).fetchall()
            print(f"\n== moves shown in the feed ({len(moves)})")
            for m in moves:
                print(f"  [{m['type']}] {m['headline']}" + (f"  — {m['why_it_matters']}" if m["why_it_matters"] else ""))
            print(f"\n== run summary\n  {run['summary']}")
    finally:
        if keep:
            print(f"\nKept for inspection: project {pid}, site {sid}. In DBeaver open schema site_{sid} "
                  f"(products, price_history, history, crawl_runs, pages).\nRemove later with:  "
                  f"delete from projects where id={pid}; drop schema site_{sid} cascade;")
        else:
            drop_site_schema(conn, sid)
            conn.execute("delete from projects where id=%s", (pid,))
            print("\n(test data removed; use --keep to inspect it in DBeaver)")
        conn_ctx.__exit__(None, None, None)
        close_pool()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", nargs="?", help="a website's home page (whole-site mode)")
    ap.add_argument("--page", metavar="URL", help="fetch and extract just this one page (no database, no AI)")
    ap.add_argument("--markdown", action="store_true", help="with --page: also print the page as markdown")
    ap.add_argument("--max-products", type=int, default=20, help="product limit for the run (default 20)")
    ap.add_argument("--pages", type=int, default=40, help="page budget for the run (default 40)")
    ap.add_argument("--no-ai", action="store_true", help="use the fixed plan instead of the Gemini agent")
    ap.add_argument("--twice", action="store_true", help="run twice to see change detection ('no changes' on run 2)")
    ap.add_argument("--keep", action="store_true", help="keep the data in the database to look at in DBeaver")
    a = ap.parse_args()
    if a.page:
        return try_page(a.page, a.markdown)
    if not a.url:
        ap.print_help()
        return 1
    if urlsplit(a.url).scheme not in ("http", "https"):
        a.url = "https://" + a.url
    return try_site(a.url, a.max_products, a.pages, not a.no_ai, a.twice, a.keep)


if __name__ == "__main__":
    sys.exit(main())
