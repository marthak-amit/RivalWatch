"""Serves the RivalWatch UI's pages (public/) with the original server's redirects and security headers."""
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, RedirectResponse, Response

from ...config import get_settings
from ...db.pool import get_conn
from .ui import session_user

router = APIRouter(include_in_schema=False)
PAGES = {"/": "index.html", "/login": "login.html", "/app": "app.html", "/admin": "admin.html",
         "/style.css": "style.css", "/common.js": "common.js", "/theme.js": "theme.js"}
HEADERS = {
    "x-content-type-options": "nosniff", "x-frame-options": "DENY", "referrer-policy": "same-origin",
    "content-security-policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                               "img-src 'self' data:; frame-ancestors 'none'",
    "cache-control": "no-store",
}


@lru_cache
def public_dir() -> Path:
    configured = get_settings().public_dir
    # editable install: src/competitor_moves/api/routers/pages.py -> repo root is 5 levels up
    return Path(configured) if configured else Path(__file__).resolve().parents[5] / "public"


def _page(request: Request, conn=Depends(get_conn)) -> Response:
    path = request.url.path
    if path in ("/app", "/admin"):
        user = session_user(request, conn)
        if path == "/app" and not user:
            return RedirectResponse("/login", 302, headers=HEADERS)
        if path == "/admin" and (not user or user["role"] != "admin"):
            return RedirectResponse("/app" if user else "/login", 302, headers=HEADERS)
    file = public_dir() / PAGES[path]
    if not file.is_file():
        return Response("UI files not found: set PUBLIC_DIR to the RivalWatch public/ folder", 404, headers=HEADERS)
    return FileResponse(file, headers=HEADERS)


for _path in PAGES:
    router.add_api_route(_path, _page, methods=["GET"])


@router.get("/favicon.ico")
def favicon():
    return Response(status_code=204, headers=HEADERS)
