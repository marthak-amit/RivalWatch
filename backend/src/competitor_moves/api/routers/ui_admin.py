"""The UI's admin panel API (/api/admin/*). Admins themselves are created and changed only in the database."""
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ...core.plans import PLANS
from ...core.security import hash_password
from ...db.pool import get_conn
from ...db.repositories import sessions as session_repo
from ...db.repositories import users
from ...services import store_sync, workspaces
from .ui import (
    EMAIL,
    StoreBody,
    StorePatch,
    UiError,
    _check_clean,
    json_only,
    public_user,
    store_patch,
    ui_admin,
)

router = APIRouter(prefix="/api/admin", tags=["ui admin"], dependencies=[Depends(json_only), Depends(ui_admin)])
ADMINS_IN_DB = "Admins are created and managed in the database, not through the API"


def row_view(r: dict) -> dict:
    return {**public_user(r), "credits": r["credits"], "creditLimit": PLANS[r["plan"]]["credits"],
            "competitors": r.get("competitors", 0)}


def _target(conn, user_id: int) -> dict:
    r = users.ui_row(conn, user_id)
    if not r:
        raise UiError(404, "User not found")
    return r


@router.get("/stats")
def stats(conn=Depends(get_conn)):
    rows = users.list_ui(conn)
    active_users = [r for r in rows if r["is_active"] and r["role"] == "user"]
    by_plan = {k: sum(1 for r in active_users if r["plan"] == k) for k in PLANS}
    week_ago = datetime.now(UTC) - timedelta(days=7)
    return {"total": len(rows), "active": sum(r["is_active"] for r in rows), "suspended": sum(not r["is_active"] for r in rows),
            "admins": sum(r["role"] == "admin" for r in rows), "byPlan": by_plan,
            "mrr": sum(n * PLANS[k]["price"] for k, n in by_plan.items()),
            "newThisWeek": sum(r["created_at"] >= week_ago for r in rows)}


@router.get("/users")
def list_users(q: str = "", plan: str = "", status: str = "", conn=Depends(get_conn)):
    q = q.strip().lower()
    out = [r for r in users.list_ui(conn)
           if (not q or q in r["email"] or q in (r["name"] or "").lower()) and (not plan or r["plan"] == plan)
           and (not status or ("active" if r["is_active"] else "suspended") == status)]
    return {"users": [row_view(r) for r in out]}


class NewUser(BaseModel):
    email: str = Field("", max_length=254)
    name: str | None = Field(None, max_length=120)
    password: str = Field("", max_length=128)
    plan: str | None = None
    role: str | None = None


@router.post("/users")
def create_user(body: NewUser, admin=Depends(ui_admin), conn=Depends(get_conn)):
    if body.role == "admin":
        raise UiError(403, ADMINS_IN_DB)
    _check_clean(body.email, body.password, body.name)
    email = body.email.strip().lower()
    if not EMAIL.match(email):
        raise UiError(400, "Enter a valid email address")
    if len(body.password) < 8:
        raise UiError(400, "Password must be at least 8 characters")
    plan = body.plan if body.plan in PLANS else "starter"
    try:
        r = users.create_ui_user(conn, email, (body.name or "").strip()[:60] or None, hash_password(body.password), plan,
                                 PLANS[plan]["credits"])
    except users.EmailTaken:
        raise UiError(400, "An account with this email already exists") from None
    users.audit(conn, admin, "create_user", r, f"user, {plan}")
    return row_view(r)


class UserPatch(BaseModel):
    plan: str | None = None
    status: str | None = None
    role: str | None = None


@router.patch("/users/{user_id}")
def update_user(user_id: int, body: UserPatch, admin=Depends(ui_admin), conn=Depends(get_conn)):
    t = _target(conn, user_id)
    if t["role"] == "admin" or body.role == "admin":
        raise UiError(403, ADMINS_IN_DB)
    fields = {}
    if body.plan is not None:
        if body.plan not in PLANS:
            raise UiError(400, "Unknown plan")
        fields.update(plan=body.plan, credits=PLANS[body.plan]["credits"])
    if body.status is not None:
        if body.status not in ("active", "suspended"):
            raise UiError(400, "Bad status")
        fields["is_active"] = body.status == "active"
    if not fields:
        return row_view(t)
    r = users.update_fields(conn, user_id, **fields)
    if body.status == "suspended":
        session_repo.end_all_for_user(conn, user_id, "disabled")
    users.audit(conn, admin, "update_user", t, ", ".join(f"{k}={v}" for k, v in body.model_dump(exclude_none=True).items()))
    return row_view(r)


@router.delete("/users/{user_id}")
def delete_user(user_id: int, admin=Depends(ui_admin), conn=Depends(get_conn)):
    t = _target(conn, user_id)
    if t["id"] == admin["id"]:
        raise UiError(400, "You can't delete your own account")
    if t["role"] == "admin":
        raise UiError(403, ADMINS_IN_DB)
    users.audit(conn, admin, "delete_user", t)
    users.delete_user(conn, user_id)  # their workspace, competitors and per-site data go with them
    return {"ok": True}


@router.post("/users/{user_id}/reset-credits")
def reset_credits(user_id: int, admin=Depends(ui_admin), conn=Depends(get_conn)):
    t = _target(conn, user_id)
    r = users.update_fields(conn, user_id, credits=PLANS[t["plan"]]["credits"])
    users.audit(conn, admin, "reset_credits", t)
    return row_view(r)


class PasswordBody(BaseModel):
    password: str = Field("", max_length=128)


@router.post("/users/{user_id}/reset-password")
def reset_password(user_id: int, body: PasswordBody, admin=Depends(ui_admin), conn=Depends(get_conn)):
    t = _target(conn, user_id)
    if t["role"] == "admin" and t["id"] != admin["id"]:
        raise UiError(403, ADMINS_IN_DB)
    _check_clean(body.password)
    if len(body.password) < 8:
        raise UiError(400, "Password must be at least 8 characters")
    conn.execute("update users set password_hash=%s where id=%s", (hash_password(body.password), user_id))
    session_repo.end_all_for_user(conn, user_id, "disabled")
    users.audit(conn, admin, "reset_password", t)
    return {"ok": True}


@router.get("/audit")
def audit(conn=Depends(get_conn)):
    return {"audit": [{"ts": a["ts"].isoformat(), "actor": a["actor_email"], "action": a["action"],
                       "target": a["target_email"] or "", "detail": a["detail"]} for a in users.audit_log(conn)]}


# ---- a client's store, managed by the admin (the agency sets it up for them) --------------------------------------

def _store_owner(conn, user_id: int) -> dict:
    t = _target(conn, user_id)
    if t["role"] == "admin":
        raise UiError(400, "Admins don't have a workspace store")
    return t


@router.get("/users/{user_id}/store")
def admin_get_store(user_id: int, conn=Depends(get_conn)):
    return {"store": store_sync.store_view(conn, _store_owner(conn, user_id))}


@router.put("/users/{user_id}/store")
def admin_put_store(user_id: int, body: StoreBody, admin=Depends(ui_admin), conn=Depends(get_conn)):
    t = _store_owner(conn, user_id)
    try:
        store_sync.connect(conn, t, body.url, store_code=body.storeCode, token=body.token, cron=body.cron)
    except workspaces.WorkspaceError as e:
        raise UiError(e.status, str(e)) from None
    users.audit(conn, admin, "store_connect", t, body.url)
    return {"store": store_sync.store_view(conn, t)}


@router.patch("/users/{user_id}/store")
def admin_patch_store(user_id: int, body: StorePatch, admin=Depends(ui_admin), conn=Depends(get_conn)):
    t = _store_owner(conn, user_id)
    store_patch(conn, t, body)
    users.audit(conn, admin, "store_update", t, ", ".join(sorted(body.model_fields_set - {"token"}) +
                                                          (["token"] if "token" in body.model_fields_set else [])))
    return {"store": store_sync.store_view(conn, t)}


@router.post("/users/{user_id}/store/sync")
def admin_sync_store(user_id: int, admin=Depends(ui_admin), conn=Depends(get_conn)):
    t = _store_owner(conn, user_id)
    view = store_sync.store_view(conn, t)
    if not view:
        raise UiError(404, "No store connected")
    users.audit(conn, admin, "store_sync", t)
    return {"queued": store_sync.queue_sync(conn, int(view["id"])), "store": store_sync.store_view(conn, t)}


@router.delete("/users/{user_id}/store")
def admin_delete_store(user_id: int, admin=Depends(ui_admin), conn=Depends(get_conn)):
    t = _store_owner(conn, user_id)
    store_sync.disconnect(conn, t)
    users.audit(conn, admin, "store_disconnect", t)
    return {"ok": True}
