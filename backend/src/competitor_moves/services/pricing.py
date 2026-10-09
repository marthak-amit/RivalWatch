"""Changing our own prices in Magento: request (with a preview), then apply; cancel; revert. Each change is a row in
price_changes, the audit log of who asked, who applied, the old and new price and what Magento said.

Applying re-reads the live price and cost from Magento, re-checks the guardrails and writes Magento first. Only when
Magento confirms is our copy of the product updated and the change logged in our store's history under the person who
applied it. A failed write changes nothing locally (status failed, with Magento's error).

What changes is Magento's base (regular) price at the default scope. A sale (special) price stays as it is.
Guardrails per workspace: one change may move the price at most max_price_change_pct (a revert may undo any change),
the margin over cost (when Magento has a cost) must stay at least min_margin_pct, and with price_admin_only only an
admin applies changes.
"""
from datetime import UTC, datetime
from decimal import Decimal

from psycopg import sql

from ..core import crypto
from ..core.ssrf import BlockedURL
from ..crawler.fetch import FetchError
from ..db.site_schema import table
from ..integrations.magento.client import MagentoError
from . import change_detection as cd
from . import comparison, store_sync, workspaces

MAGENTO_ERRORS = (MagentoError, FetchError, BlockedURL, crypto.CryptoError)
SELECT = """select c.*, r.email as requested_by, a.email as applied_by_email, o.email as client
            from price_changes c join projects p on p.id = c.project_id left join users o on o.id = p.owner_user_id
            left join users r on r.id = c.user_id left join users a on a.id = c.applied_by"""


class PriceError(workspaces.WorkspaceError):
    pass


def settings_of(project: dict) -> dict:
    return {"maxChangePct": float(project["max_price_change_pct"]), "minMarginPct": float(project["min_margin_pct"]),
            "adminOnly": project["price_admin_only"]}


def update_settings(conn, project: dict, actor: dict, *, max_change_pct=None, min_margin_pct=None, admin_only=None) -> dict:
    if actor["role"] != "admin" and (project["price_admin_only"] or admin_only is not None):
        raise PriceError("Only your agency can change the price guardrails for this store", 403)
    row = conn.execute("update projects set max_price_change_pct=coalesce(%s, max_price_change_pct), "
                       "min_margin_pct=coalesce(%s, min_margin_pct), price_admin_only=coalesce(%s, price_admin_only) "
                       "where id=%s returning *", (max_change_pct, min_margin_pct, admin_only, project["id"])).fetchone()
    return settings_of(row)


def _pct(new: Decimal, old: Decimal) -> float:
    return round(float((new - old) / old * 100), 1)


def margin_pct(price: Decimal, cost: Decimal) -> float:
    return round(float((price - cost) / price * 100), 1)


def view(r: dict) -> dict:
    return {"id": str(r["id"]), "sku": r["sku"], "title": r["title"], "url": r["product_url"], "currency": r["currency"],
            "oldPrice": cd.money(r["old_price"]), "newPrice": cd.money(r["new_price"]),
            "changePct": _pct(r["new_price"], r["old_price"]) if r["old_price"] else None, "status": r["status"],
            "error": r["push_error"], "requestedBy": r["requested_by"], "appliedBy": r["applied_by_email"],
            "client": r["client"], "reverts": str(r["reverts"]) if r["reverts"] else None,
            "createdAt": r["created_at"].isoformat(), "appliedAt": r["applied_at"].isoformat() if r["applied_at"] else None}


def list_changes(conn, *, project_id: int | None = None, status: str | None = None, limit: int = 100) -> list[dict]:
    rows = conn.execute(SELECT + " where (%(p)s::int is null or c.project_id = %(p)s) and (%(s)s::text is null or c.status = %(s)s) "
                        "order by c.id desc limit %(l)s", {"p": project_id, "s": status, "l": limit}).fetchall()
    return [view(r) for r in rows]


def _one(conn, change_id: int) -> dict:
    return view(conn.execute(SELECT + " where c.id = %s", (change_id,)).fetchone())


def project_of(conn, change_id: int) -> dict:
    row = conn.execute("select p.* from projects p join price_changes c on c.project_id = p.id where c.id = %s",
                       (change_id,)).fetchone()
    if not row:
        raise PriceError("Price change not found", 404)
    return row


def _store(conn, project: dict) -> dict:
    store = store_sync.connection(conn, project["id"])
    if not store or not store["site_id"]:
        raise PriceError("Connect your Magento store first", 404)
    return store


def _product(conn, site_id: int, sku: str) -> dict:
    row = conn.execute(sql.SQL("select * from {} where sku = %s and removed_at is null order by last_seen_at desc limit 1")
                       .format(table(site_id, "products")), (sku,)).fetchone()
    if not row:
        raise PriceError(f"SKU {sku} isn't in your store's catalog (sync the store if it's new)", 404)
    return row


def _regular(row: dict) -> Decimal:
    return row["compare_at_price"] or row["price"]  # compare_at is stored only while a sale price is lower


def _check(s: dict, current: Decimal, new: Decimal, cost: Decimal | None, *, revert: bool = False) -> None:
    if new == current:
        raise PriceError(f"The price is already {new}")
    if not revert and abs(_pct(new, current)) > s["maxChangePct"]:
        raise PriceError(f"A change of {_pct(new, current)}% is more than the {s['maxChangePct']:g}% one change may move "
                         f"the price (price guardrails)")
    if cost is not None and margin_pct(new, cost) < s["minMarginPct"]:
        raise PriceError(f"At {new} the margin over cost ({cost}) would be {margin_pct(new, cost)}%, below the "
                         f"{s['minMarginPct']:g}% minimum")


def _shown(row: dict, regular: Decimal) -> tuple[Decimal, Decimal | None]:
    """(price shown, compare-at) once the regular price is `regular`: a sale price stays while it is lower."""
    sale = row["price"] if row["compare_at_price"] is not None else None
    if sale is not None and sale < regular:
        return sale, regular
    return regular, None


def _position(conn, project: dict, store: dict, row: dict, before: Decimal, after: Decimal) -> dict:
    """Where the shown price sits against competitors before and after: the medians of the same jewellery type, and the
    same product (exact SKU/barcode) where a competitor sells it. gap = how much more (+) or less (-) they charge."""
    ours = conn.execute("select * from sites where id=%s", (store["site_id"],)).fetchone()
    cmp = comparison.compare(conn, project, ours, workspaces.competitors(conn, project["id"]), group_by="type")
    kind = comparison.product_type(row["title"], row["category"])
    group = next((r for r in cmp["rows"] if r["key"]["type"] == kind), None)
    b, a = float(before), float(after)
    out = {"type": kind, "competitors": [], "sameProduct": [], "notes": cmp["notes"]}
    for t in (group or {}).get("competitors", []):
        if "medianInOurCurrency" in t:
            m = t["medianInOurCurrency"]
            out["competitors"].append({"name": t["name"], "median": m, "gapBefore": comparison.gap_pct(m, b),
                                       "gapAfter": comparison.gap_pct(m, a)})
    for m in cmp["matches"]:
        theirs = m["theirs"]["priceInOurCurrency"]
        if m["ours"]["url"] == row["url"] and theirs is not None:
            out["sameProduct"].append({"name": m["competitor"], "price": theirs, "url": m["theirs"]["url"],
                                       "gapBefore": comparison.gap_pct(theirs, b), "gapAfter": comparison.gap_pct(theirs, a)})
    if not group:
        out["notes"].append(f"Not enough {kind or 'similar'} products on both sides to compare medians.")
    return out


def request(conn, project: dict, actor: dict, sku: str, new_price: Decimal, *, reverts: int | None = None) -> dict:
    """Create a pending change (replacing an earlier pending one for the same SKU) and return it with its preview."""
    store = _store(conn, project)
    row = _product(conn, store["site_id"], sku)
    s, warnings = settings_of(project), []
    live, cost, live_error = None, None, None
    try:
        live, cost = store_sync.magento_client(store).price_info(sku)
    except MAGENTO_ERRORS as e:
        live_error = str(e)[:300]
        warnings.append(f"Couldn't read the live price and cost from Magento ({live_error}): showing the last synced "
                        f"price. Applying needs an integration token with Catalog → Inventory → Products access.")
    current = live or _regular(row)
    _check(s, current, new_price, cost, revert=reverts is not None)
    with conn.transaction():
        conn.execute("update price_changes set status='cancelled' where project_id=%s and sku=%s and status='pending'",
                     (project["id"], sku))
        change_id = conn.execute(
            "insert into price_changes(project_id, sku, title, product_url, currency, old_price, new_price, user_id, reverts) "
            "values (%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id",
            (project["id"], sku, row["title"], row["url"], store["currency"] or row["currency"], current, new_price,
             actor["id"], reverts)).fetchone()["id"]
    shown_before, _ = _shown(row, current)
    shown_after, _ = _shown(row, new_price)
    if shown_after != new_price:
        warnings.append(f"This product has a sale price ({row['price']}) that stays in place: shoppers keep paying "
                        f"{shown_after}; the regular price shown as 'was' becomes {new_price}.")
    if cost is None and not live_error:
        warnings.append("Magento has no cost for this product, so the margin can't be checked.")
    preview = {"currentPrice": cd.money(current), "newPrice": cd.money(new_price), "changePct": _pct(new_price, current),
               "priceSource": "magento" if live is not None else "catalog", "salePrice": cd.money(row["price"]) if row["compare_at_price"] else None,
               "shownBefore": cd.money(shown_before), "shownAfter": cd.money(shown_after), "cost": cd.money(cost),
               "marginBefore": margin_pct(current, cost) if cost else None, "marginAfter": margin_pct(new_price, cost) if cost else None,
               "position": _position(conn, project, store, row, shown_before, shown_after), "warnings": warnings,
               "settings": s, "canApply": not s["adminOnly"] or actor["role"] == "admin"}
    return {"change": _one(conn, change_id), "preview": preview}


def apply(conn, project: dict, change_id: int, actor: dict) -> dict:
    """Write the price to Magento; on success update our copy and history. Raises PriceError(502) if Magento refuses."""
    s = settings_of(project)
    if s["adminOnly"] and actor["role"] != "admin":
        raise PriceError("Your agency applies price changes for this store: this change is waiting for them", 403)
    store = _store(conn, project)
    error = None
    with conn.transaction():
        ch = conn.execute("select * from price_changes where id=%s and project_id=%s for update",
                          (change_id, project["id"])).fetchone()
        if not ch:
            raise PriceError("Price change not found", 404)
        if ch["status"] != "pending":
            raise PriceError(f"This change is already {ch['status']}", 409)
        row = _product(conn, store["site_id"], ch["sku"])
        client, live = store_sync.magento_client(store), None
        try:
            live, cost = client.price_info(ch["sku"])
            if live is None:
                raise MagentoError(f"Magento has no price for SKU {ch['sku']}")
            _check(s, live, ch["new_price"], cost, revert=ch["reverts"] is not None)  # PriceError: stays pending
            client.set_price(ch["sku"], ch["new_price"])
        except MAGENTO_ERRORS as e:
            error = str(e)[:300]
        if error:
            conn.execute("update price_changes set status='failed', push_error=%s, applied_by=%s, old_price=coalesce(%s, old_price) "
                         "where id=%s", (error, actor["id"], live, change_id))
        else:
            now = datetime.now(UTC)
            price, compare_at = _shown(row, ch["new_price"])
            events = cd.record_product(conn, store["site_id"], None, {**row, "price": price, "compare_at_price": compare_at},
                                       now=now, window_start=now, prev_full_coverage=True, user_id=actor["id"])
            if not events:  # on sale: only the regular ("was") price moved
                cd.store_events(conn, store["site_id"], None, [{
                    "type": "product_updated", "category": row["category"], "occurred_at": now,
                    "before": {"compare_at_price": cd.money(_regular(row))}, "after": {"compare_at_price": cd.money(ch["new_price"])}}],
                    row["id"], user_id=actor["id"], product=row)
            conn.execute("update price_changes set status='applied', pushed=true, push_error=null, applied_by=%s, "
                         "applied_at=now(), old_price=%s where id=%s", (actor["id"], live, change_id))
    if error:
        raise PriceError(f"Magento didn't take the change, nothing was changed: {error}", 502)
    return _one(conn, change_id)


def cancel(conn, project: dict, change_id: int) -> dict:
    if not conn.execute("update price_changes set status='cancelled' where id=%s and project_id=%s and status='pending' "
                        "returning id", (change_id, project["id"])).fetchone():
        raise PriceError("No pending change with that id", 404)
    return _one(conn, change_id)


def revert(conn, project: dict, change_id: int, actor: dict) -> dict:
    """Put back the price an applied change replaced, as a change of its own (applied at once if the actor may)."""
    ch = conn.execute("select * from price_changes where id=%s and project_id=%s", (change_id, project["id"])).fetchone()
    if not ch or ch["status"] != "applied":
        raise PriceError("Only an applied change can be reverted", 404 if not ch else 409)
    out = request(conn, project, actor, ch["sku"], ch["old_price"], reverts=ch["id"])
    if out["preview"]["canApply"]:
        out["change"] = apply(conn, project, int(out["change"]["id"]), actor)
    return out
