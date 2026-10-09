from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware

from ..config import get_settings
from ..db.pool import close_pool, get_pool
from .routers import admin_users, auth_admin, auth_user, crawl_api, health, moves


@asynccontextmanager
async def lifespan(_: FastAPI):
    get_pool()
    yield
    close_pool()


def create_app() -> FastAPI:
    settings = get_settings()  # fails fast if DATABASE_URL / JWT_SECRET are missing or too short
    app = FastAPI(title="Competitor Moves", lifespan=lifespan)
    # Bearer tokens (no cookies), so a wildcard origin is safe; set CORS_ORIGINS to restrict.
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins.split(","),
                       allow_methods=["*"], allow_headers=["*"])
    for r in (health.router, auth_user.router, auth_admin.router, admin_users.router, moves.router, crawl_api.router):
        app.include_router(r)

    @app.exception_handler(crawl_api.ApiError)
    async def _api_error(_: Request, e: crawl_api.ApiError):
        return crawl_api.error_response(e.status, e.type, e.message)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, e: RequestValidationError):
        if not request.url.path.startswith("/v1/"):
            return await request_validation_exception_handler(request, e)
        first = e.errors()[0] if e.errors() else {}
        where = ".".join(str(x) for x in first.get("loc", [])[1:])
        return crawl_api.error_response(400, "validation_error", f"{where}: {first.get('msg', 'invalid request')}")

    return app


app = create_app()
