"""The RivalWatch UI's API (/api/*): cookie session and the same response shapes as the UI's original server.js.

Errors are {"error": message}. State-changing calls must send JSON (cross-site forms can't), and the session cookie
is HttpOnly + SameSite=Lax: the same CSRF defence the original server used.
"""
import re
import secrets
import time
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, StrictBool, field_validator

from ...agents.compare import graph as compare_graph
from ...config import get_settings
from ...core import ratelimit
from ...core.plans import ANNUAL_DISCOUNT, PLANS
from ...core.security import DUMMY_HASH, TTL, hash_password, verify_password
from ...db.pool import get_conn
from ...db.repositories import users
from ...services import comparison, dashboard, sessions, store_sync, workspaces
from ...services.product_search import SORT_SQL, Filters, search
from ..deps import authenticate
from ..schemas.auth import no_control_chars

COOKIE = "rw_session"
EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$")


class UiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def json_only(request: Request) -> None:
    if request.method not in ("GET", "HEAD") and not request.headers.get("content-type", "").startswith("application/json"):
        raise UiError(415, "JSON body required")


router = APIRouter(prefix="/api", tags=["ui"], dependencies=[Depends(json_only)])


class Limiter:
    """Login/signup attempts per IP+email. ponytail: in-process, so per worker; use Redis if the API scales out."""

    def __init__(self, max_hits: int = 10, window_sec: int = 300):
        self.max, self.window, self.hits = max_hits, window_sec, defaultdict(list)

    def hit(self, key: str) -> bool:
        now = time.monotonic()
        self.hits[key] = [t for t in self.hits[key] if now - t < self.window] + [now]
        return len(self.hits[key]) <= self.max

    def reset(self, key: str) -> None:
        self.hits.pop(key, None)


limiter = Limiter()


# ---- session helpers ------------------------------------------------------------------------------------------

def session_user(request: Request, conn) -> dict | None:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    try:
        return authenticate(conn, token)
    except HTTPException:
        return None


def ui_user(request: Request, conn=Depends(get_conn)) -> dict:
    u = session_user(request, conn)
    if not u:
        raise UiError(401, "Not signed in")
    return u


def ui_admin(u: dict = Depends(ui_user)) -> dict:
    if u["role"] != "admin":
        raise UiError(403, "Admin only")
    return u


def public_user(r: dict) -> dict:
    return {"id": r["id"], "email": r["email"], "name": r["name"] or r["email"].split("@")[0], "role": r["role"],
            "plan": r["plan"], "status": "active" if r["is_active"] else "suspended",
            "createdAt": r["created_at"].isoformat(), "lastLoginAt": r["last_login_at"].isoformat() if r["last_login_at"] else None}


def me_view(conn, user_id: int) -> dict:
    r = users.ui_row(conn, user_id)
    return {**public_user(r), "apiKey": r["api_key"], "plan": r["plan"], "planInfo": PLANS[r["plan"]], "credits": r["credits"]}


def _set_cookie(response: Response, request: Request, token: str, role: str) -> None:
    secure = request.headers.get("x-forwarded-proto") == "https" or request.url.scheme == "https"
    response.set_cookie(COOKIE, token, max_age=TTL[role], httponly=True, samesite="lax", secure=secure, path="/")


# ---- auth -----------------------------------------------------------------------------------------------------

class LoginBody(BaseModel):
    email: str = Field("", max_length=254)
    password: str = Field("", max_length=128)

    @field_validator("email", "password")
    @classmethod
    def _no_control_chars(cls, v: str) -> str:
        # a NUL byte makes Postgres raise a DataError (a 500); other control characters are never legitimate here
        if re.search(r"[\x00-\x1f\x7f]", v):
            raise ValueError("contains invalid characters")
        return v


class SignupBody(LoginBody):
    name: str | None = Field(None, max_length=120)
    plan: str | None = None


@router.get("/session")
def get_session(request: Request, conn=Depends(get_conn)):
    u = session_user(request, conn)
    return {"user": me_view(conn, u["id"]) if u else None}


@router.get("/plans")
def plans():
    # `backend` tells the pages what this server supports (they hide what it doesn't): competitors are always real
    # websites here, and admins are managed in the database
    return {"plans": list(PLANS.values()), "annualDiscount": ANNUAL_DISCOUNT, "demo": {"user": None, "admin": None},
            "backend": {"enabled": True, "demo": False, "signupName": True, "planChanges": True, "rotateKey": True,
                        "adminPlanEdit": True, "adminRoleEdit": False, "adminResetCredits": True, "adminResetPassword": True}}


def _rate_key(request: Request, email: str) -> str:
    return f"{request.client.host if request.client else '?'}|{email.strip().lower()}"


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _check_clean(*values: str | None) -> None:
    try:
        for v in values:
            no_control_chars(v)
    except ValueError:
        raise UiError(400, "Email and password must not contain control characters") from None


def _signed_in(conn, request: Request, response: Response, row: dict) -> dict:
    started = sessions.start(conn, row["id"], row["role"], request)
    _set_cookie(response, request, started["token"], row["role"])
    return {"redirect": "/admin" if row["role"] == "admin" else "/app", "user": public_user(users.ui_row(conn, row["id"]))}


@router.post("/auth/login")
def login(body: LoginBody, request: Request, response: Response, conn=Depends(get_conn)):
    _check_clean(body.email, body.password)
    key = ratelimit.login_key(_ip(request), body.email)
    if ratelimit.logins.blocked(key):
        raise UiError(429, "Too many attempts. Try again in a few minutes.")
    row = users.find_any_role(conn, body.email)
    ok = verify_password(body.password, row["password_hash"] if row else DUMMY_HASH)
    if not (row and ok):
        ratelimit.logins.fail(key)
        raise UiError(401, "Incorrect email or password")
    ratelimit.logins.clear(key)
    if not row["is_active"]:
        raise UiError(403, "This account is suspended. Contact support.")
    limiter.reset(_rate_key(request, body.email))  # only failures count towards the limit
    return _signed_in(conn, request, response, row)


@router.post("/auth/signup")
def signup(body: SignupBody, request: Request, response: Response, conn=Depends(get_conn)):
    _check_clean(body.email, body.password, body.name)
    if ratelimit.signups.blocked(_ip(request) or "?"):
        raise UiError(429, "Too many sign-ups from this address. Try again in a few minutes.")
    ratelimit.signups.fail(_ip(request) or "?")
    email = body.email.strip().lower()
    if not EMAIL.match(email) or len(email) > 120:
        raise UiError(400, "Enter a valid email address")
    if len(body.password) < 8:
        raise UiError(400, "Password must be at least 8 characters")
    plan = body.plan if body.plan in PLANS else "starter"
    try:
        row = users.create_ui_user(conn, email, (body.name or "").strip()[:60] or None, hash_password(body.password),
                                   plan, PLANS[plan]["credits"])
    except users.EmailTaken:
        raise UiError(400, "An account with this email already exists") from None
    users.audit(conn, row, "signup", row, f"plan {plan}")
    workspaces.get_or_create(conn, row)
    return _signed_in(conn, request, response, row)


@router.post("/auth/logout")
def logout(request: Request, response: Response, conn=Depends(get_conn)):
    u = session_user(request, conn)
    if u:
        sessions.end(conn, u["jti"], "logout")
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


# ---- account --------------------------------------------------------------------------------------------------

class PlanBody(BaseModel):
    plan: str


@router.get("/me")
def me(u=Depends(ui_user), conn=Depends(get_conn)):
    return me_view(conn, u["id"])


@router.post("/me/plan")
def change_plan(body: PlanBody, u=Depends(ui_user), conn=Depends(get_conn)):
    if body.plan not in PLANS:
        raise UiError(400, "Unknown plan")
    users.update_fields(conn, u["id"], plan=body.plan, credits=PLANS[body.plan]["credits"])  # mock checkout: new allowance
    users.audit(conn, u, "plan_change", u, body.plan)
    return me_view(conn, u["id"])


@router.post("/me/rotate-key")
def rotate_key(u=Depends(ui_user), conn=Depends(get_conn)):
    users.update_fields(conn, u["id"], api_key="rw_" + secrets.token_hex(16))
    return me_view(conn, u["id"])


# ---- dashboard ------------------------------------------------------------------------------------------------

@router.get("/state")
def state(u=Depends(ui_user), conn=Depends(get_conn)):
    return dashboard.state(conn, u)


@router.post("/crawl")
def crawl(u=Depends(ui_user), conn=Depends(get_conn)):
    """Queue a crawl of every enabled competitor and wait (bounded) so 'Crawl complete' is true when it says so."""
    project = workspaces.get_or_create(conn, u)
    sites = [s for s in workspaces.competitors(conn, project["id"]) if s["status"] == "active"]
    if not sites:
        return {"skipped": True, "changes": 0}
    started = datetime.now(UTC)
    queued = workspaces.crawl_now(conn, u)
    deadline = time.monotonic() + get_settings().crawl_now_wait_sec
    ids = [str(s["id"]) for s in sites]
    pending = True
    while time.monotonic() < deadline:
        pending = conn.execute("select 1 from jobs where type='crawl_site' and status in ('queued','running') "
                               "and payload->>'site_id' = any(%s) limit 1", (ids,)).fetchone() is not None
        if not pending:
            break
        time.sleep(1)
    new = dashboard.changes(conn, sites, since=started - timedelta(seconds=1))
    return {"skipped": False, "queued": queued, "pending": pending, "changes": len(new)}


class StoreBody(BaseModel):
    url: str = Field(max_length=500)
    storeCode: str | None = Field(None, max_length=40)
    token: str | None = Field(None, max_length=500)
    cron: str | None = None


@router.get("/store")
def get_store(u=Depends(ui_user), conn=Depends(get_conn)):
    """Our own store (Magento): connection, product count and sync status; null when not connected."""
    return {"store": store_sync.store_view(conn, u)}


@router.put("/store")
def put_store(body: StoreBody, u=Depends(ui_user), conn=Depends(get_conn)):
    """Connect (or change) our Magento store. The token is optional (public catalog data needs none), stored encrypted
    and never returned. The first sync starts at once."""
    try:
        store_sync.connect(conn, u, body.url, store_code=body.storeCode, token=body.token, cron=body.cron)
    except workspaces.WorkspaceError as e:
        raise UiError(e.status, str(e)) from None
    return {"store": store_sync.store_view(conn, u)}


class StorePatch(BaseModel):
    storeCode: str | None = Field(None, max_length=40)
    token: str | None = Field(None, max_length=500)  # "" or null removes it; leave it out to keep it
    cron: str | None = None
    enabled: StrictBool | None = None


def store_patch(conn, user: dict, body: StorePatch) -> None:
    kw = {"store_code": body.storeCode, "cron": body.cron, "enabled": body.enabled}
    if "token" in body.model_fields_set:
        kw["token"] = body.token
    try:
        store_sync.update(conn, user, **kw)
    except workspaces.WorkspaceError as e:
        raise UiError(e.status, str(e)) from None


@router.patch("/store")
def patch_store(body: StorePatch, u=Depends(ui_user), conn=Depends(get_conn)):
    """Change the store's settings without reconnecting: store view, token, sync schedule, pause/resume."""
    store_patch(conn, u, body)
    return {"store": store_sync.store_view(conn, u)}


@router.post("/store/sync")
def sync_store(u=Depends(ui_user), conn=Depends(get_conn)):
    view = store_sync.store_view(conn, u)
    if not view:
        raise UiError(404, "No store connected")
    return {"queued": store_sync.queue_sync(conn, int(view["id"])), "store": store_sync.store_view(conn, u)}


@router.delete("/store")
def delete_store(u=Depends(ui_user), conn=Depends(get_conn)):
    store_sync.disconnect(conn, u)
    return {"ok": True}


class CompetitorBody(BaseModel):
    name: str | None = Field(None, max_length=120)
    url: str = Field("", max_length=2000)
    maxProducts: int | None = None
    pageBudget: int | None = None
    categories: list[dict] | None = None
    sort: str | None = None
    cron: str | None = None
    enabled: StrictBool | None = None
    crawlSettings: dict | None = None


class CompetitorPatch(BaseModel):
    name: str | None = Field(None, max_length=120)
    enabled: StrictBool | None = None
    maxProducts: int | None = None
    pageBudget: int | None = None
    categories: list[dict] | None = None
    sort: str | None = None
    cron: str | None = None
    crawlSettings: dict | None = None


def _settings(b: BaseModel) -> dict:
    d = b.model_dump(exclude_unset=True)
    names = {"maxProducts": "max_products", "pageBudget": "page_budget", "crawlSettings": "crawl_settings"}
    return {names.get(k, k): v for k, v in d.items() if k != "url"}


@router.post("/competitors")
def add_competitor(body: CompetitorBody, u=Depends(ui_user), conn=Depends(get_conn)):
    try:
        site = workspaces.add_competitor(conn, u, body.url, **_settings(body))
    except workspaces.WorkspaceError as e:
        raise UiError(e.status, str(e)) from None
    return {"ok": True, "id": str(site["id"]), "competitor": dashboard.competitor_view(conn, site)}


@router.patch("/competitors/{competitor_id}")
def update_competitor(competitor_id: str, body: CompetitorPatch, u=Depends(ui_user), conn=Depends(get_conn)):
    try:
        site = workspaces.update_competitor(conn, u, competitor_id, **_settings(body))
    except workspaces.WorkspaceError as e:
        raise UiError(e.status, str(e)) from None
    return {"ok": True, "enabled": site["status"] != "paused", "competitor": dashboard.competitor_view(conn, site)}


@router.delete("/competitors/{competitor_id}")
def remove_competitor(competitor_id: str, u=Depends(ui_user), conn=Depends(get_conn)):
    try:
        workspaces.remove_competitor(conn, u, competitor_id)
    except workspaces.WorkspaceError as e:
        raise UiError(e.status, str(e)) from None
    return {"ok": True}


@router.get("/competitors/{competitor_id}/categories")
def competitor_categories(competitor_id: str, u=Depends(ui_user), conn=Depends(get_conn)):
    """The competitor's own menu (from its last crawl): what the crawl can be scoped to."""
    try:
        return {"categories": workspaces.menu_categories(conn, u, competitor_id)}
    except workspaces.WorkspaceError as e:
        raise UiError(e.status, str(e)) from None


def _clean_id(v: str | None) -> str:
    return v if v and re.fullmatch(r"\w{1,20}", v) else ""  # competitor ids are short \w tokens, as in the UI


def _report_inputs(conn, u: dict):
    project = workspaces.get_or_create(conn, u)
    sites = workspaces.competitors(conn, project["id"])
    since = datetime.now(UTC) - timedelta(days=61)
    return dashboard.changes(conn, sites, since=since), dashboard.crawl_log(conn, sites, since)


@router.get("/reports")
def reports(period: str = "week", competitor: str | None = None, u=Depends(ui_user), conn=Depends(get_conn)):
    chs, runs = _report_inputs(conn, u)
    return dashboard.build_report(chs, runs, "month" if period == "month" else "week", competitor_id=_clean_id(competitor))


class SummaryBody(BaseModel):
    period: str = "week"
    competitor: str | None = None


@router.post("/reports/summary")
def report_summary(body: SummaryBody, u=Depends(ui_user), conn=Depends(get_conn)):
    chs, runs = _report_inputs(conn, u)
    comp = _clean_id(body.competitor)
    r = dashboard.build_report(chs, runs, "month" if body.period == "month" else "week", competitor_id=comp)
    if not r["total"]:
        raise UiError(400, "No changes in this period to summarise")
    in_range = [c for c in chs if c["ts"][:10] >= r["from"] and (not comp or c["competitorId"] == comp)]
    if get_settings().gemini_api_key and not comp:  # the analyst, over this period (not stored)
        project = workspaces.get_or_create(conn, u)
        since = datetime.fromisoformat(r["from"]).replace(tzinfo=UTC)
        d = compare_graph.run_digest(conn, project, since=since, store=False, force=True)
        if d:
            return {"summary": d["summary"], "actions": d["actions"], "source": d["source"]}
    return dashboard.rules_digest(in_range)


class CompareSettings(BaseModel):
    minPrice: float | None = Field(None, ge=0, le=1_000_000)
    minGroupSize: int | None = Field(None, ge=1, le=50)
    fxRates: dict[str, float] | None = None  # e.g. {"GBP": 1.27}: 1 GBP = 1.27 of our store's currency


def _compare_inputs(conn, u: dict):
    project = workspaces.get_or_create(conn, u)
    ours = conn.execute("select * from sites where project_id=%s and role='ours'", (project["id"],)).fetchone()
    return project, ours, workspaces.competitors(conn, project["id"])


@router.get("/comparison")
def comparison_view(group_by: str = "type", category: str | None = None, competitor: str | None = None,
                    u=Depends(ui_user), conn=Depends(get_conn)):
    """Our prices against competitors: by jewellery type (and metal, stone or carat band), exact SKU matches, and what
    only competitors sell. group_by: type | type,metal | type,stone | type,metal,stone | type,carat."""
    if group_by not in comparison.GROUP_BY:
        raise UiError(400, f"group_by must be one of: {', '.join(comparison.GROUP_BY)}")
    project, ours, comps = _compare_inputs(conn, u)
    if competitor:
        comps = [c for c in comps if str(c["id"]) == competitor]
    return comparison.compare(conn, project, ours, comps, group_by=group_by, category=category)


@router.get("/comparison/settings")
def get_compare_settings(u=Depends(ui_user), conn=Depends(get_conn)):
    return comparison.settings_of(workspaces.get_or_create(conn, u))


@router.patch("/comparison/settings")
def patch_compare_settings(body: CompareSettings, u=Depends(ui_user), conn=Depends(get_conn)):
    """minPrice skips cheap test items; minGroupSize is how many products a group needs on each side; fxRates convert
    competitor currencies into our store's currency (without a rate, those prices are shown but not compared)."""
    project = workspaces.get_or_create(conn, u)
    current = dict(project["compare_settings"] or {})
    if body.minPrice is not None:
        current["min_price"] = body.minPrice
    if body.minGroupSize is not None:
        current["min_group_size"] = body.minGroupSize
    if body.fxRates is not None:
        rates = {}
        for code, rate in body.fxRates.items():
            if not re.fullmatch(r"[A-Z]{3}", code) or not 0 < rate < 1_000_000:
                raise UiError(400, f"fxRates: '{code}' needs a 3-letter currency code and a positive rate")
            rates[code] = rate
        current["fx_rates"] = rates
    conn.execute("update projects set compare_settings=%s where id=%s", (Jsonb(current), project["id"]))
    return comparison.settings_of({**project, "compare_settings": current})


@router.get("/products")
def products(competitor: str | None = None, category: str | None = None, metal: str | None = None,
             gem: str | None = None, stone: str | None = None, min_price: float | None = Query(None, ge=0),
             max_price: float | None = Query(None, ge=0), on_sale: bool | None = None, in_stock: bool | None = None,
             q: str | None = Query(None, max_length=100), sort: str = "relevance", limit: int = Query(50, ge=1, le=200),
             offset: int = Query(0, ge=0, le=10_000), u=Depends(ui_user), conn=Depends(get_conn)):
    """Products collected from your competitors (or `competitor=ours` for our own store), filtered like a shop's own
    category page."""
    if sort not in SORT_SQL:
        raise UiError(400, f"sort must be one of: {', '.join(SORT_SQL)}")
    project = workspaces.get_or_create(conn, u)
    if competitor == "ours":  # our own store's catalog
        sites = conn.execute("select * from sites where project_id=%s and role='ours'", (project["id"],)).fetchall()
    else:
        sites = workspaces.competitors(conn, project["id"])
        if competitor:
            sites = [s for s in sites if str(s["id"]) == competitor]
    return search(conn, sites, Filters(category, metal, gem, stone, min_price, max_price, on_sale, in_stock, q, sort),
                  limit=limit, offset=offset)


@router.api_route("/test/{rest:path}", methods=["GET", "POST"], include_in_schema=False)
def live_demo(rest: str, u=Depends(ui_user)):
    raise UiError(404, "The live demo isn't available on this server: competitors here are real websites")
