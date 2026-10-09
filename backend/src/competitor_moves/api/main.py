from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..config import get_settings
from ..db.pool import close_pool, get_pool
from .routers import admin_users, auth_admin, auth_user, health, moves


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
    for r in (health.router, auth_user.router, auth_admin.router, admin_users.router, moves.router):
        app.include_router(r)
    return app


app = create_app()
