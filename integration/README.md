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

## Login / logout only

`npm run test:auth` runs `auth.integration.mjs` against whatever server `NODE_URL` points at. It detects standalone vs backend mode itself.
In standalone mode it uses the built-in demo accounts; with a backend, pass the admin you created:

```bash
NODE_URL=http://127.0.0.1:3000 ADMIN_EMAIL=boss@rivalwatch.test ADMIN_PASSWORD='admin-pass-12345' npm run test:auth
```

Note: the login limiter counts failures per account+IP (10 per 5 minutes), so re-running against the same server immediately is fine, but
hammering one account with wrong passwords will (correctly) return 429 until the window passes.


## Native mode (the backend serves `/api/*`)

`npm run test:native` runs `native.integration.mjs` (161 checks) against the real backend, worker and Postgres, through this Node server in native mode:
accounts and plan changes, a real competitor crawl of a stand-in Shopify-style shop (`shop.mjs`, port 3998, editable through `/__edit?action=drop|rise|sale|oos|restock|add|reset`),
competitor settings and scope, product search and facets, change detection (price drop, on sale, out of stock, new product, back in stock), reports,
pause/resume, the `/v1` proxy, the admin panel and the audit log. It starts `shop.mjs` (competitor, :3998) and `magento.mjs` (our store, :3997, token `good-token`) itself and also covers connecting and syncing the store, the comparison, price changes (preview/apply/revert/guardrails/a refusing Magento) and the agency approval flow.

Start the backend pieces as above, but allow both stand-in sites through the SSRF guard and also run the scheduler if you want scheduled crawls:

```bash
export HARNESS_ALLOW_LOCAL=127.0.0.1:3999,127.0.0.1:3998,127.0.0.1:3997
export APP_ENCRYPTION_KEY=$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')   # needed to store a store token
python integration/run_backend_api.py & python integration/run_backend_worker.py &
BACKEND_URL=http://127.0.0.1:8000 npm start &          # prints "native mode"
npm run test:native
```

`npm run test:integration` still exercises the adapter: run it with `BACKEND_MODE=bff npm start`.

## Browser tests

`npm run test:browser` drives the real pages with Playwright against the same stack (set `PLAYWRIGHT_MODULE` to a Playwright install and `CHROMIUM` to a browser if the defaults don't resolve; screenshots go to `SHOTS_DIR`, default the temp folder):
`landing.browser.mjs` (the landing page, mobile and reduced motion), `native.browser.mjs` (dashboard: settings, products, change types) and `native.browser.prices.mjs` (comparison, the price dialog, price changes, admin approvals). The last two start from `shop.mjs` and `magento.mjs`, so run those first (`node integration/shop.mjs & node integration/magento.mjs &`).
