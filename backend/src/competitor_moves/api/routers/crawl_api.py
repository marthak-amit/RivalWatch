"""SocialCrawl-style public crawl API (x-api-key auth, one response envelope, credits), as the UI documents it."""
import uuid

from fastapi import APIRouter, Depends, Header
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from ...db.pool import get_conn
from ...db.repositories import credits, users
from ...services import web_crawl

router = APIRouter(prefix="/v1", tags=["crawl api"])


class ApiError(Exception):
    def __init__(self, status: int, type_: str, message: str):
        super().__init__(message)
        self.status, self.type, self.message = status, type_, message


def request_id() -> str:
    return "req_" + uuid.uuid4().hex[:12]


def error_response(status: int, type_: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={
        "success": False, "error": {"type": type_, "message": message, "status": status}, "request_id": request_id()})


def ok(status: int, endpoint: str, data, used: int, remaining: int) -> JSONResponse:
    return JSONResponse(status_code=status, content=jsonable_encoder({
        "success": True, "platform": "web", "endpoint": endpoint, "data": data, "credits_used": used,
        "credits_remaining": remaining, "request_id": request_id(), "cached": False}))


def api_user(x_api_key: str | None = Header(None), conn=Depends(get_conn)) -> dict:
    u = users.find_by_api_key(conn, x_api_key or "")
    if not u:
        raise ApiError(401, "auth_error", "Missing or invalid x-api-key header")
    return u


@router.get("/credits/balance")
def balance(u=Depends(api_user), conn=Depends(get_conn)):
    left = credits.balance(conn, u["id"])
    return ok(200, "/v1/credits/balance", {"balance": left}, 0, left)


@router.get("/web/scrape")
def scrape(url: str | None = None, u=Depends(api_user), conn=Depends(get_conn)):
    if not url:
        raise ApiError(400, "validation_error", "`url` is required")
    before = credits.balance(conn, u["id"])
    try:
        data = web_crawl.scrape(conn, u["id"], url)
    except credits.InsufficientCredits:
        raise ApiError(402, "insufficient_credits", "insufficient credits") from None
    except web_crawl.ScrapeError as e:
        left = credits.balance(conn, u["id"])
        raise ApiError(400, "request_error", f"{e} (credits used: {before - left})") from None
    left = credits.balance(conn, u["id"])
    return ok(200, "/v1/web/scrape", data, before - left, left)


@router.post("/web/crawl")
def start_crawl(body: web_crawl.CrawlRequest, u=Depends(api_user), conn=Depends(get_conn)):
    try:
        job = web_crawl.start_crawl(conn, u["id"], body)
    except web_crawl.ScrapeError as e:
        raise ApiError(400, "request_error", str(e)) from None
    return ok(202, "/v1/web/crawl", job, 0, credits.balance(conn, u["id"]))


@router.get("/web/crawl/{job_id}")
def get_crawl(job_id: str, u=Depends(api_user), conn=Depends(get_conn)):
    job = web_crawl.get_job(conn, u["id"], job_id)
    if not job:
        raise ApiError(404, "not_found", "Unknown job_id")
    return ok(200, f"/v1/web/crawl/{job_id}", job, 0, credits.balance(conn, u["id"]))


@router.api_route("/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
def unknown(rest: str, u=Depends(api_user)):
    raise ApiError(404, "not_found", "Unknown endpoint")
