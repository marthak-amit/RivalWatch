import os

# a database of their own: the compose worker polls `competitor` and would claim the tests' jobs
os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/competitor_test")
os.environ.setdefault("JWT_SECRET", "test-secret-test-secret-test-secret-123")
os.environ["CRAWL_DELAY_SEC"] = "0"
os.environ["GEMINI_API_KEY"] = ""  # tests never call the real model (overrides .env)
os.environ["CRAWL_NOW_WAIT_SEC"] = "0"

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from competitor_moves.api.main import app
from competitor_moves.core.security import hash_password
from competitor_moves.db.pool import close_pool, get_pool


@pytest.fixture(scope="session", autouse=True)
def test_database():
    """Create the test database on first use and bring it to the latest migration."""
    import psycopg
    from alembic import command
    from alembic.config import Config

    url = os.environ["DATABASE_URL"]
    base, name = url.rsplit("/", 1)
    with psycopg.connect(f"{base}/postgres", autocommit=True) as c:
        if not c.execute("select 1 from pg_database where datname=%s", (name,)).fetchone():
            c.execute(f'create database "{name}"')
    command.upgrade(Config(str(Path(__file__).parents[1] / "alembic.ini")), "head")


@pytest.fixture
def conn():
    with get_pool().connection() as c:
        yield c


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c
    with get_pool().connection() as c:
        from competitor_moves.db.site_schema import drop_site_schema
        for row in c.execute("select s.id from sites s join projects p on p.id = s.project_id join users u on u.id = p.owner_user_id "
                             "where u.email like '%@test.local'").fetchall():
            drop_site_schema(c, row["id"])
        c.execute("delete from jobs")  # the test database's queue: nothing left for the next test's worker.run_once
        c.execute("delete from audit_log where actor_email like '%@test.local'")
        c.execute("delete from users where email like '%@test.local'")
    from competitor_moves.core import ratelimit
    ratelimit.logins.hits.clear()
    ratelimit.signups.hits.clear()
    close_pool()


@pytest.fixture
def make_admin():
    def _make(email="boss@test.local", password="adminpass123"):
        with get_pool().connection() as c:
            c.execute("insert into users(email,password_hash,role) values (%s,%s,'admin')",
                      (email, hash_password(password)))
        return email, password
    return _make


@pytest.fixture
def site(monkeypatch):
    """A local web server standing in for a competitor site. routes: path -> (status, headers, body)."""
    from competitor_moves.core import ssrf
    from competitor_moves.services import web_crawl

    routes: dict[str, tuple[int, dict, bytes | str]] = {}
    hits: list[str] = []
    dynamic: list = []  # optional callables path -> (status, headers, body) | None, tried when no route matches
    posts: list = []  # (path, body) of every POST, the latest last (a dynamic handler reads it for the body)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            posts.append((self.path, self.rfile.read(int(self.headers.get("content-length") or 0)).decode()))
            self.do_GET()

        def do_GET(self):
            hits.append(self.path)
            found = routes.get(self.path) or next((r for f in dynamic if (r := f(self.path, self.headers))), None)
            status, headers, body = found or (404, {"content-type": "text/plain"}, "not found")
            body = body.encode() if isinstance(body, str) else body
            self.send_response(status)
            for k, v in {"content-type": "text/html; charset=utf-8", **headers}.items():
                self.send_header(k, v)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host = f"127.0.0.1:{srv.server_port}"
    monkeypatch.setattr(ssrf, "ALLOW_LOCAL", {host})
    web_crawl._robots_cache.clear()
    yield SimpleNamespace(base=f"http://{host}", routes=routes, hits=hits, dynamic=dynamic, posts=posts)
    srv.shutdown()


@pytest.fixture
def make_site(conn):
    """Create a project + site + its site_<id> schema. Optional owner (pays credits). Cleans up afterwards."""
    from urllib.parse import urlsplit

    from competitor_moves.db.site_schema import create_site_schema, drop_site_schema

    made = []

    def _make(url, *, owner_email=None, credits=100, role="competitor", **cols):
        owner = None
        if owner_email:
            owner = conn.execute("insert into users(email,password_hash,credits) values (%s,'x',%s) returning id",
                                 (owner_email, credits)).fetchone()["id"]
        pid = conn.execute("insert into projects(name,url,owner_user_id) values ('test-project',%s,%s) returning id",
                           (url, owner)).fetchone()["id"]
        sid = conn.execute("insert into sites(project_id,domain,url,role,schema_name) values (%s,%s,%s,%s,md5(random()::text)) "
                           "returning id", (pid, urlsplit(url).netloc, url, role)).fetchone()["id"]
        conn.execute("update sites set schema_name=%s where id=%s", (f"site_{sid}", sid))
        for k, v in cols.items():
            conn.execute(f"update sites set {k}=%s where id=%s", (v, sid))
        create_site_schema(conn, sid)
        made.append((pid, sid, owner))
        return sid

    yield _make
    for pid, sid, owner in made:
        drop_site_schema(conn, sid)
        conn.execute("delete from jobs where payload->>'site_id' = %s", (str(sid),))
        conn.execute("delete from projects where id=%s", (pid,))
        if owner:
            conn.execute("delete from users where id=%s", (owner,))
