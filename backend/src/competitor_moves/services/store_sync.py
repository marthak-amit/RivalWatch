"""Our own store (Magento): connect it to a workspace and keep its catalog in sync.

The store is a site with role 'ours' and its own site_<id> schema, so its products, price history and change history
work exactly like a competitor's, and everything that compares against "our" catalog reads the same tables.
"""
from datetime import UTC, datetime, timedelta

from psycopg.types.json import Jsonb

from ..config import DEFAULT_CRON, get_settings
from ..core import crypto
from ..core.ssrf import BlockedURL, check_url
from ..crawler.fetch import FetchError
from ..db.repositories import site_data
from ..db.site_schema import create_site_schema, drop_site_schema
from ..integrations.magento.client import MagentoClient, MagentoError, to_product
from . import change_detection as cd
from . import workspaces

MAX_PAGES = 400  # 40,000 products at 100 per page; a bigger catalog is synced in part and says so


class StoreError(workspaces.WorkspaceError):
    pass


def magento_client(conn_row: dict) -> MagentoClient:
    token = crypto.decrypt(conn_row["token_encrypted"]) if conn_row["token_encrypted"] else None
    return MagentoClient(conn_row["base_url"], user_agent=get_settings().user_agent, store_code=conn_row["store_code"],
                         token=token)


def connection(conn, project_id: int) -> dict | None:
    return conn.execute("select * from magento_connections where project_id=%s", (project_id,)).fetchone()


def connect(conn, user: dict, url: str, *, store_code: str | None = None, token: str | None = None,
            cron: str | None = None) -> dict:
    """Check the store answers, then create (or update) the workspace's 'our store' site and queue a sync."""
    project = workspaces.get_or_create(conn, user)
    base = workspaces.normalize_url(url).rstrip("/")
    try:
        check_url(base)
    except BlockedURL as e:
        raise StoreError(str(e)) from None
    if cron is not None:
        workspaces._validate({"cron": cron})
    existing = connection(conn, project["id"])
    if token:
        token_enc = crypto.encrypt(token)
    elif existing and existing["base_url"] == base:
        token_enc = existing["token_encrypted"]  # same store: keep the token given earlier
    else:
        token_enc = None
    probe = {"base_url": base, "store_code": (store_code or "default").strip()[:40] or "default", "token_encrypted": token_enc}
    try:
        info = magento_client(probe).store()
    except (MagentoError, FetchError, BlockedURL, crypto.CryptoError) as e:
        raise StoreError(f"Couldn't read the store's catalog API: {e}") from None
    with conn.transaction():
        site = conn.execute("select * from sites where project_id=%s and role='ours'", (project["id"],)).fetchone()
        domain = base.split("://", 1)[1].split("/")[0]
        if site is None:
            site = conn.execute(
                "insert into sites(project_id, domain, url, role, name, schema_name, platform, cron, next_run_at) "
                "values (%s,%s,%s,'ours',%s,md5(random()::text),'magento',%s,now()) returning *",
                (project["id"], domain, base + "/", info.get("store_name") or domain, cron or DEFAULT_CRON)).fetchone()
            site = conn.execute("update sites set schema_name=%s where id=%s returning *", (f"site_{site['id']}", site["id"])).fetchone()
        else:
            site = conn.execute("update sites set domain=%s, url=%s, name=%s, status='active', cron=coalesce(%s, cron) "
                                "where id=%s returning *",
                                (domain, base + "/", info.get("store_name") or domain, cron, site["id"])).fetchone()
        create_site_schema(conn, site["id"])
        conn.execute("""
            insert into magento_connections(project_id, site_id, base_url, store_code, token_encrypted, store_name, currency,
                                            status, last_error, updated_at)
            values (%s,%s,%s,%s,%s,%s,%s,'connected',null,now())
            on conflict (project_id) do update set site_id=excluded.site_id, base_url=excluded.base_url,
              store_code=excluded.store_code, token_encrypted=excluded.token_encrypted, store_name=excluded.store_name,
              currency=excluded.currency, status='connected', last_error=null, updated_at=now()""",
                     (project["id"], site["id"], base, probe["store_code"], token_enc, info.get("store_name"),
                      info.get("base_currency_code")))
    queue_sync(conn, site["id"])
    return site


_KEEP = object()


def update(conn, user: dict, *, store_code: str | None = None, token=_KEEP, cron: str | None = None,
           enabled: bool | None = None) -> None:
    """Change settings without reconnecting. token: a string sets it, "" or None removes it, omitted keeps it.
    A new store view or token is checked against the store before it is saved; enabled=False pauses the sync."""
    project = workspaces.get_or_create(conn, user)
    store = connection(conn, project["id"])
    if not store:
        raise StoreError("No store connected", 404)
    if cron is not None:
        workspaces._validate({"cron": cron})
    if store_code is not None or token is not _KEEP:
        new_code = (store_code or store["store_code"]).strip()[:40] or "default"
        new_token = store["token_encrypted"] if token is _KEEP else (crypto.encrypt(token) if token else None)
        probe = {"base_url": store["base_url"], "store_code": new_code, "token_encrypted": new_token}
        try:
            info = magento_client(probe).store()
        except (MagentoError, FetchError, BlockedURL, crypto.CryptoError) as e:
            raise StoreError(f"Couldn't read the store's catalog API with these settings: {e}") from None
        conn.execute("update magento_connections set store_code=%s, token_encrypted=%s, currency=%s, updated_at=now() "
                     "where project_id=%s", (new_code, new_token, info.get("base_currency_code"), project["id"]))
    sets, values = [], []
    if cron is not None:
        sets.append("cron=%s")
        values.append(cron.strip())
    if enabled is not None:
        sets.append("status=%s")
        values.append("active" if enabled else "paused")
    if sets:
        conn.execute(f"update sites set {', '.join(sets)} where id=%s", [*values, store["site_id"]])
    if enabled:
        queue_sync(conn, store["site_id"])


def disconnect(conn, user: dict) -> None:
    project = workspaces.get_or_create(conn, user)
    site = conn.execute("select id from sites where project_id=%s and role='ours'", (project["id"],)).fetchone()
    with conn.transaction():
        conn.execute("delete from magento_connections where project_id=%s", (project["id"],))
        if site:
            conn.execute("delete from jobs where type='sync_store' and payload->>'site_id'=%s and status='queued'", (str(site["id"]),))
            conn.execute("delete from sites where id=%s", (site["id"],))
            drop_site_schema(conn, site["id"])


def queue_sync(conn, site_id: int) -> bool:
    return conn.execute("insert into jobs(type, payload) values ('sync_store', %s) on conflict do nothing returning id",
                        (Jsonb({"site_id": site_id}),)).fetchone() is not None


def run_sync(conn, site_id: int) -> dict:
    """Read the whole catalog, store it with change detection, and record the run (worker handler for 'sync_store')."""
    site = conn.execute("select s.*, p.id as pid from sites s join projects p on p.id = s.project_id where s.id=%s",
                        (site_id,)).fetchone()
    store = connection(conn, site["pid"]) if site else None
    if not site or not store:
        return {"site_id": site_id, "status": "skipped", "summary": "store not connected"}
    now = datetime.now(UTC)
    prev = site_data.last_run(conn, site_id)
    run_id = site_data.start_run(conn, site_id, "sync")
    client, seen, events, total, complete, error = None, set(), [], None, False, None
    try:
        client = magento_client(store)
        page, pages = 1, 1
        while page <= min(pages, MAX_PAGES):
            items, total, pages = client.products(page)
            for raw in items:
                if not raw.get("url_key"):
                    continue
                item = {**to_product(raw, store["base_url"]), "source_url": f"{store['base_url']}/graphql"}
                if item["url"] in seen:
                    continue
                events += cd.record_product(conn, site_id, run_id, item, now=now, window_start=now - timedelta(days=7),
                                            prev_full_coverage=bool(prev and prev["full_coverage"]))
                seen.add(item["url"])
            page += 1
        complete = pages <= MAX_PAGES
        if complete:  # every product was read: the ones not seen (twice in a row) were removed from the store
            events += cd.mark_missing(conn, site_id, run_id, seen, now=now)
    except (MagentoError, FetchError, BlockedURL, crypto.CryptoError) as e:
        error = str(e)[:300]
    counts = {}
    for e in events:
        counts[e["type"]] = counts.get(e["type"], 0) + 1
    status = "failed" if error and not seen else "partial" if error or not complete else "ok"
    summary = (f"Could not sync {site['domain']}: {error}" if status == "failed" else
               f"Synced {len(seen)} of {total} products from {site['domain']}" + (
                   f"; {sum(counts.values())} changes" if counts else "; no changes") + (f" (stopped: {error})" if error else ""))
    site_data.finish_run(conn, site_id, run_id, status=status, pages_fetched=client.requests if client else 0,
                         products_seen=len(seen), est_total_products=total, full_coverage=complete and not error,
                         changes=counts, summary=summary, error=error)
    conn.execute("update sites set last_run_at=now(), last_error=%s where id=%s", (error if status == "failed" else None, site_id))
    conn.execute("update magento_connections set last_sync_at=now(), status=%s, last_error=%s where project_id=%s",
                 ("error" if status == "failed" else "connected", error, site["pid"]))
    return {"site_id": site_id, "run_id": run_id, "status": status, "summary": summary, "products": len(seen),
            "events": sum(counts.values())}


def run_job(conn, job: dict) -> dict:
    return run_sync(conn, int(job["payload"]["site_id"]))


def store_view(conn, user: dict) -> dict | None:
    project = workspaces.get_or_create(conn, user)
    store = connection(conn, project["id"])
    if not store:
        return None
    last = site_data.last_run(conn, store["site_id"]) if store["site_id"] else None
    count = site_data.product_count(conn, store["site_id"]) if store["site_id"] else 0
    running = conn.execute("select 1 from jobs where type='sync_store' and status in ('queued','running') "
                           "and payload->>'site_id'=%s limit 1", (str(store["site_id"]),)).fetchone() is not None
    site = conn.execute("select cron, next_run_at, status from sites where id=%s", (store["site_id"],)).fetchone()
    return {"id": str(store["site_id"]), "platform": "magento", "url": store["base_url"], "name": store["store_name"],
            "storeCode": store["store_code"], "currency": store["currency"], "hasToken": bool(store["token_encrypted"]),
            "status": store["status"], "error": store["last_error"], "syncing": running, "productCount": count,
            "lastSync": store["last_sync_at"].isoformat() if store["last_sync_at"] else None,
            "lastSummary": last["summary"] if last else None, "cron": site["cron"] if site else None,
            "enabled": bool(site) and site["status"] != "paused",
            "nextSync": site["next_run_at"].isoformat() if site and site["next_run_at"] else None}
