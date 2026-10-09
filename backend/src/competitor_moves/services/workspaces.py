"""One workspace (project) per user: their competitor sites and each site's crawl settings."""
import re
from urllib.parse import urlsplit, urlunsplit

from croniter import croniter
from psycopg import sql
from psycopg.types.json import Jsonb

from ..config import DEFAULT_CRON, get_settings
from ..core.plans import PLANS
from ..core.ssrf import BlockedURL, check_url
from ..crawler.extract import same_site
from ..db.site_schema import create_site_schema, drop_site_schema, table

SORTS = ("relevance", "newest", "price_asc", "price_desc", "discount")
EDITABLE = ("name", "enabled", "max_products", "page_budget", "categories", "sort", "cron", "crawl_settings")
# per-site crawl settings: key -> (type, min, max); bounds keep one site from hogging the worker
CRAWL_SETTINGS = {"delay_sec": (float, 0.2, 10), "time_limit_sec": (int, 60, 3600), "browser": (str, None, None),
                  "browser_pages": (int, 0, 200), "product_meta": (bool, None, None), "ai_extract": (bool, None, None),
                  "ai_extract_cap": (int, 0, 100)}


def crawl_settings(site_settings: dict | None) -> dict:
    """Effective crawl settings for a site: its own overrides on top of the server defaults."""
    s, own = get_settings(), site_settings or {}
    defaults = {"delay_sec": s.crawl_delay_sec, "time_limit_sec": s.crawl_time_limit_sec, "browser": s.browser,
                "browser_pages": s.browser_pages, "product_meta": s.product_page_meta, "ai_extract": s.ai_extract,
                "ai_extract_cap": s.ai_extract_cap}
    return {k: own.get(k, v) for k, v in defaults.items()}


def _validate_crawl_settings(v) -> dict:
    if not isinstance(v, dict):
        raise WorkspaceError("`crawl_settings` must be an object")
    out = {}
    for k, val in v.items():
        if k not in CRAWL_SETTINGS:
            raise WorkspaceError(f"Unknown crawl setting `{k}`. Allowed: {', '.join(CRAWL_SETTINGS)}")
        if val is None:  # null = go back to the server default
            continue
        typ, lo, hi = CRAWL_SETTINGS[k]
        if k == "browser":
            if val not in ("off", "auto"):
                raise WorkspaceError("`browser` must be 'off' or 'auto'")
        elif typ is bool:
            if not isinstance(val, bool):
                raise WorkspaceError(f"`{k}` must be true or false")
        else:
            if isinstance(val, bool) or not isinstance(val, (int, float)) or (typ is int and int(val) != val) or not lo <= val <= hi:
                raise WorkspaceError(f"`{k}` must be a number between {lo} and {hi}")
            val = typ(val)
        out[k] = val
    return out


class WorkspaceError(ValueError):
    """A request that can't be done; `status` is the HTTP status the API should answer with."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def get_or_create(conn, user: dict) -> dict:
    row = conn.execute("select * from projects where owner_user_id=%s", (user["id"],)).fetchone()
    if row:
        return row
    return conn.execute(
        "insert into projects(name, url, owner_user_id) values (%s, '', %s) "
        "on conflict (owner_user_id) where owner_user_id is not null do update set name = excluded.name returning *",
        (f"{user['email']}'s workspace", user["id"])).fetchone()


def competitors(conn, project_id: int) -> list[dict]:
    return conn.execute("select * from sites where project_id=%s and role='competitor' and status <> 'removed' order by id",
                        (project_id,)).fetchall()


def _site(conn, project_id: int, site_id) -> dict:
    try:
        sid = int(site_id)
    except (TypeError, ValueError):
        raise WorkspaceError("Competitor not found", 404) from None
    row = conn.execute("select * from sites where id=%s and project_id=%s and role='competitor' and status <> 'removed'",
                       (sid, project_id)).fetchone()
    if not row:
        raise WorkspaceError("Competitor not found", 404)
    return row


def normalize_url(raw: str) -> str:
    raw = (raw or "").strip()
    if raw and "://" not in raw:
        raw = "https://" + raw
    try:
        p = urlsplit(raw)
        p.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError:
        raise WorkspaceError("Enter a valid http(s) URL") from None
    if p.scheme not in ("http", "https") or not p.hostname or not re.fullmatch(r"[a-z0-9.-]+\.[a-z0-9-]+|\[[0-9a-f:]+\]|[0-9.]+",
                                                                                 p.hostname):
        raise WorkspaceError("Enter a valid http(s) URL")
    return urlunsplit((p.scheme, p.netloc.lower(), p.path or "/", "", ""))


def _validate(fields: dict) -> dict:
    s, out = get_settings(), {}
    for k, v in fields.items():
        if v is None or k not in EDITABLE:
            continue
        if k == "name":
            v = str(v).strip()[:60]
            if not v:
                raise WorkspaceError("Name can't be empty")
        elif k == "enabled":
            if not isinstance(v, bool):
                raise WorkspaceError("`enabled` must be true or false")
        elif k in ("max_products", "page_budget"):
            if isinstance(v, bool) or not isinstance(v, int):
                raise WorkspaceError(f"`{k}` must be a whole number")
            top = s.max_products_cap if k == "max_products" else 5000
            if not 1 <= v <= top:
                raise WorkspaceError(f"`{k}` must be between 1 and {top}")
        elif k == "sort":
            if v not in SORTS:
                raise WorkspaceError(f"`sort` must be one of: {', '.join(SORTS)}")
        elif k == "cron":
            if not isinstance(v, str) or not croniter.is_valid(v.strip()):
                raise WorkspaceError("`cron` must be a cron expression, e.g. '0 2 * * *' (daily at 02:00 UTC)")
            v = v.strip()
        elif k == "crawl_settings":
            v = _validate_crawl_settings(v)
        elif k == "categories":
            if not isinstance(v, list) or len(v) > 20 or not all(
                    isinstance(c, dict) and isinstance(c.get("name"), str) and isinstance(c.get("url"), str) for c in v):
                raise WorkspaceError("`categories` must be a list of {name, url} picked from the site's menu (max 20)")
            v = [{"name": c["name"].strip()[:60], "url": c["url"].strip()} for c in v if c["name"].strip()]
        out[k] = v
    return out


def add_competitor(conn, user: dict, url: str, name: str | None = None, **settings) -> dict:
    project = get_or_create(conn, user)
    url = normalize_url(url)
    try:
        check_url(url)
    except BlockedURL as e:
        raise WorkspaceError(str(e)) from None
    fields = _validate(settings)
    plan = PLANS[conn.execute("select plan from users where id=%s", (user["id"],)).fetchone()["plan"]]
    existing = competitors(conn, project["id"])
    if len(existing) >= plan["competitors"]:
        raise WorkspaceError(f"Your {plan['name']} plan allows {plan['competitors']} competitors. Upgrade to add more.", 403)
    domain = urlsplit(url).netloc
    if any(e["domain"] == domain for e in existing):
        raise WorkspaceError("Already monitoring this site")
    for c in fields.get("categories", []):
        if not same_site(c["url"], url):
            raise WorkspaceError(f"Category \"{c['name']}\" is not on {domain}")
    with conn.transaction():
        site = conn.execute(
            "insert into sites(project_id, domain, url, role, name, schema_name, status, max_products, page_budget, "
            "categories, sort, cron, crawl_settings, next_run_at) "
            "values (%s,%s,%s,'competitor',%s,md5(random()::text),%s,%s,%s,%s,%s,%s,%s,now()) "
            "returning *",
            (project["id"], domain, url, (name or "").strip()[:60] or (urlsplit(url).hostname or domain).removeprefix("www."),
             "active" if fields.get("enabled", True) else "paused", fields.get("max_products"), fields.get("page_budget"),
             Jsonb(fields.get("categories", [])), fields.get("sort", "relevance"), fields.get("cron", DEFAULT_CRON),
             Jsonb(fields.get("crawl_settings", {})))).fetchone()
        site = conn.execute("update sites set schema_name=%s where id=%s returning *", (f"site_{site['id']}", site["id"])).fetchone()
        create_site_schema(conn, site["id"])
    if site["status"] == "active":
        queue_crawl(conn, site["id"])
    return site


def update_competitor(conn, user: dict, site_id, **changes) -> dict:
    project = get_or_create(conn, user)
    site = _site(conn, project["id"], site_id)
    fields = _validate(changes)
    for c in fields.get("categories", []):
        if not same_site(c["url"], site["url"]):
            raise WorkspaceError(f"Category \"{c['name']}\" is not on {site['domain']}")
    sets, values = [], []
    if "enabled" in fields:
        sets.append("status = %s")
        values.append("active" if fields.pop("enabled") else "paused")
    if "crawl_settings" in fields:  # merge: only the keys sent change; null resets a key to the default
        raw = changes.get("crawl_settings") or {}
        merged = {**(site["crawl_settings"] or {}), **fields.pop("crawl_settings")}
        fields["crawl_settings"] = {k: v for k, v in merged.items() if raw.get(k, 0) is not None}
    for k, v in fields.items():
        sets.append(f"{k} = %s")
        values.append(Jsonb(v) if k in ("categories", "crawl_settings") else v)
    if not sets:
        return site
    updated = conn.execute(f"update sites set {', '.join(sets)} where id=%s returning *", [*values, site["id"]]).fetchone()
    if updated["status"] == "active" and site["status"] != "active":
        queue_crawl(conn, site["id"])  # catch up on what changed while it was paused or blocked
    return updated


def remove_competitor(conn, user: dict, site_id) -> None:
    project = get_or_create(conn, user)
    site = _site(conn, project["id"], site_id)
    with conn.transaction():
        conn.execute("delete from jobs where type='crawl_site' and payload->>'site_id'=%s and status='queued'", (str(site["id"]),))
        conn.execute("delete from sites where id=%s", (site["id"],))
        drop_site_schema(conn, site["id"])


def menu_categories(conn, user: dict, site_id) -> list[dict]:
    """The site's menu as the last crawl saw it, for picking a crawl scope."""
    project = get_or_create(conn, user)
    site = _site(conn, project["id"], site_id)
    row = conn.execute(sql.SQL("select home from {} where home is not null order by id desc limit 1").format(
        table(site["id"], "crawl_runs"))).fetchone()
    nav = ((row or {}).get("home") or {}).get("nav") or []
    return [{"name": n["name"], "url": n["url"]} for n in nav if re.search(r"\w", n["name"])]


def queue_crawl(conn, site_id: int) -> bool:
    """Queue a crawl now unless one is already queued or running for this site."""
    return conn.execute("insert into jobs(type, payload) values ('crawl_site', %s) on conflict do nothing returning id",
                        (Jsonb({"site_id": site_id}),)).fetchone() is not None


def crawl_now(conn, user: dict) -> int:
    project = get_or_create(conn, user)
    return sum(queue_crawl(conn, s["id"]) for s in competitors(conn, project["id"]) if s["status"] == "active")
