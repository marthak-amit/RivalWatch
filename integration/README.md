# Backend integration test

End-to-end check of this Node server against the **real** Python backend: accounts and sessions, real crawls through the
worker, credits, change detection, robots.txt and SSRF errors, the `/v1` proxy, and the admin panel (53 checks).
It needs live services, so it is not part of `npm test`.

## Run it (no Docker needed)

```bash
# Postgres 16 with a UTF8 database (SQL_ASCII makes psycopg return bytes and alembic fails)
createdb -E UTF8 -T template0 competitor

export DATABASE_URL=postgresql://postgres@127.0.0.1:5433/competitor
export JWT_SECRET=local-dev-secret-local-dev-secret-0123456789 CRAWL_DELAY_SEC=0
export HARNESS_ALLOW_LOCAL=127.0.0.1:3999        # lets the stand-in competitor site through the backend's SSRF guard

(cd backend && PYTHONPATH=src alembic upgrade head)
python integration/run_backend_api.py &           # API on :8000
python integration/run_backend_worker.py &        # crawl worker
(cd backend && PYTHONPATH=src python scripts/create_admin.py boss@rivalwatch.test 'admin-pass-12345') | psql "$DATABASE_URL"

BACKEND_URL=http://127.0.0.1:8000 npm start &     # the UI server on :3000
npm run test:integration
```

`run_backend_*.py` start the backend unmodified, except that they add the stand-in site (`rival.mjs`, port 3999) to the
SSRF allow-list, the same way the backend's own tests do. Nothing in `backend/` is changed.

Environment: `NODE_URL` (default `http://127.0.0.1:3000`), `ADMIN_EMAIL`, `ADMIN_PASSWORD`.
