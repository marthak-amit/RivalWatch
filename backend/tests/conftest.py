import os

os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/competitor")
os.environ.setdefault("JWT_SECRET", "test-secret-test-secret-test-secret-123")

import pytest
from fastapi.testclient import TestClient

from competitor_moves.api.main import app
from competitor_moves.core.security import hash_password
from competitor_moves.db.pool import close_pool, get_pool


@pytest.fixture
def conn():
    with get_pool().connection() as c:
        yield c


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c
    with get_pool().connection() as c:
        c.execute("delete from users where email like '%@test.local'")
    close_pool()


@pytest.fixture
def make_admin():
    def _make(email="boss@test.local", password="adminpass123"):
        with get_pool().connection() as c:
            c.execute("insert into users(email,password_hash,role) values (%s,%s,'admin')",
                      (email, hash_password(password)))
        return email, password
    return _make
