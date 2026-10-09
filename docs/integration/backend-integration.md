# Integrating the UI with the Python backend

The UI talks to one contract, `/api/*`. The Python backend (`backend/`) now serves that contract itself, so there are three ways to run:

| Mode | When | What happens |
|---|---|---|
| **Direct** | `docker compose up` in `backend/`, open http://localhost:8031 | The backend serves `public/` and `/api/*`. Nothing else is needed. |
| **Native** | `BACKEND_URL=... npm start`, backend answers `GET /api/plans` with `backend.enabled` | `server.js` serves the pages and forwards `/api/*` and `/v1/*` unchanged (cookies, client IP via `X-Forwarded-For`). The startup log prints `native mode`. |
| **Adapter** | `BACKEND_URL=... npm start` against an older backend with only `/user`, `/admin`, `/v1` (or `BACKEND_MODE=bff`) | `server.js` maps each `/api/*` route to the backend call where one exists and runs the monitoring pipeline in Node memory (sections below). |
| Standalone | no `BACKEND_URL` | demo mode with built-in accounts and editable test competitors |

`BACKEND_MODE=auto|native|bff` (default `auto`). The probe is repeated every second while the backend is unreachable and every 10 s if it answered without `/api/*`.

## What the UI shows in native mode

Features are gated by `GET /api/plans -> backend` so the standalone demo and the adapter hide what they can't do (the adapter sets `competitorSettings`, `products`, `store`, `adminStore` to `false`).

* **Competitor cards**: live crawl progress ("Crawling · 12 pages · 7 products"), "Catalog: 4 collected of ~6 · shopify", partial-crawl marker, blocked/paused notices; every card keeps the same size.
* **Settings** (per competitor): name, product limit (1-5000), page budget, sort (site order, newest, price asc/desc, biggest discount), schedule (presets or a cron expression, UTC),
  scope (tick categories from `GET /api/competitors/{id}/categories`), advanced crawl settings. Only changed keys are sent on `PATCH`; clearing a numeric crawl setting sends `null` (back to the server default). The same fields are available (collapsed) when adding a competitor.
* **Products**: `GET /api/products` with the facets feeding the dropdowns, 300 ms debounced search, price range, on sale / in stock, sort, paging (50), and an All competitors / Our store switch.
* **Changes / reports**: labels and tags for `on_sale`, `out_of_stock`, `back_in_stock`, `product_updated`, `new_category`, `page_changed`; product counts use `snapshot.productCount` (the snapshot's `products` map only holds the 25 most recent).
* **Comparison** (`GET /api/comparison`): price positioning per jewellery type (group-by: type, +metal, +stone, +carat), our median against each competitor with the gap in words ("36.5% cheaper than us"; `gapPct < 0` = competitor cheaper) and a price scale per row, a "They sell, we don't" list, shared-SKU matches with a *Change price* button, and a settings form (minimum price, minimum group size, currency rates). Without a rate, prices in another currency are shown but never compared; the backend's notes say so.
* **Price changes in Magento** (the shared `priceDialog` in `common.js`): *Change price* on our products (Products → Our store) or a shared SKU → new price → **preview** (live price and cost from Magento, margin before/after, what shoppers pay when a sale price stays, warnings, position against competitor medians and the same product) → **Apply** (Magento is written first; if it refuses, the change is `failed` and nothing changed) → **Revert**. The *Price changes* page lists every change (status, who asked, who applied, Magento's error), filters by status, applies/cancels/reverts, and edits the guardrails (largest single change, lowest margin; locked when `adminOnly`).
* **Digests**: the source badge shows `Gemini`, `Gemini + rules` or `rule-based`, and an action backed by evidence shows how many data points it cites.
* **Our store** (appears when `GET /api/store` answers 200): connect (`PUT`), edit (`PATCH`: token omitted keeps it, `""` removes it, URL is fixed), pause/resume (`enabled`), sync now, disconnect, and "View our products" (`competitor=ours`).
  **Admin**: a *Store* button per customer opens the same controls on `/api/admin/users/{id}/store` (shown when `backend.enabled` and `adminStore !== false`), plus that client's **price section**: guardrails, the *only the agency applies price changes* switch (`adminOnly`), a SKU box to request a change for them, and their last changes with Apply/Cancel/Revert. A **Waiting for approval** card at the top of the admin page lists every client's pending change (`GET /api/admin/prices?status=pending`) with Apply and Cancel.

Defensive details: snapshots with a missing profile, promotions or pages are normalised before rendering; the *Add a competitor* form is kept across repaints so a refresh never wipes what you typed;
the dashboard polls every 2 s while a crawl runs and every 5 s otherwise.

## Adapter mode (older backends)

The Node server becomes a thin backend-for-frontend: the browser still talks to the same `/api/*`, and each route is
mapped to the real backend call where one exists.

## Run it

```bash
# 1. backend: Postgres, migrations, API (:8000) and the crawl worker (see backend/docker-compose.yml)
cd backend && cp .env.example .env        # set JWT_SECRET (32+ chars)
docker compose up -d --build

# 2. an admin (admins are created in the database only; compose publishes Postgres on :5433)
python backend/scripts/create_admin.py boss@example.com 'a-long-password' | psql postgresql://postgres:postgres@localhost:5433/competitor

# 3. the UI (optional: the backend already serves it on :8031)
BACKEND_URL=http://localhost:8031 npm start        # http://localhost:3000 (native mode)
```

> I verified the integration by running the same pieces directly (Postgres 16, `alembic upgrade head`, `uvicorn`, and
> `python -m competitor_moves.workers.worker`), not through `docker compose`. The backend's own 88 tests pass in that setup.

| Env | Meaning |
|---|---|
| `BACKEND_URL` | Turns the integration on. Unset = standalone demo. |
| `DEMO_COMPETITORS` | `1`/`0`. Whether new workspaces start with the three editable demo competitors. Default: on standalone, **off** with a backend (real accounts start empty and the *Live demo* tab is hidden; set `DEMO_COMPETITORS=1` to bring it back). |
| `DEMO_DATA` | `1` opts in to fabricated data (sample report history, fake admin customers). Default off, so every screen shows real data. |
| `DEMO_USER`, `DEMO_ADMIN` | Optional `email:password` shown as one-click buttons on the login page. |

Worker required: crawl jobs sit in `queued` until the backend's worker container is running. The UI says
"Is the backend crawl worker running?" if a job doesn't finish in two minutes.

## What goes where

| UI route | With a backend | Notes |
|---|---|---|
| `POST /api/auth/login` | `POST /user/login`, then `POST /admin/login` on 401 | one form for both roles; `redirect` is `/admin` or `/app` |
| `POST /api/auth/signup` | `POST /user/signup` | `name` and `plan` are accepted by the form but **ignored by the backend** |
| `POST /api/auth/logout` | `POST /user/logout` or `/admin/logout` | the token is revoked server-side too |
| `GET /api/session`, `GET /api/me` | `GET /user/me` | cached 4 s; suspended/expired/idle sessions return `null` / 401 |
| `GET /api/plans` | local (same data as `core/plans.py`) | plus a `backend` object of capability flags the UI reads |
| `POST /api/me/plan`, `/api/me/rotate-key` | — | **501**: no backend endpoint yet. UI disables/hides them |
| `/v1/*` | proxied verbatim to the backend `/v1/*` | per-user key, credits, robots.txt, SSRF checks all backend-side |
| `GET /api/admin/users` | `GET /admin/users` | `plan`, `credits`, `name` are `null`/derived (not in the list yet); adds `activeNow`, `sessions`, `totalSessionSec` |
| `PATCH /api/admin/users/:id {status}` | `PATCH /admin/users/{id} {is_active}` | also ends that user's sessions; `plan`/`role` edits → 501 |
| `DELETE /api/admin/users/:id` | `DELETE /admin/users/{id}` | |
| `POST /api/admin/users` | `POST /user/signup` (+ logout of the session it opens) | `role: admin` → 403 |
| `/api/admin/stats` | computed from `/admin/users` | no plan mix / MRR (`byPlan: null`); shows "Signed in now" instead |
| `reset-credits`, `reset-password` | — | **501** |
| `/api/admin/audit` | local, in memory | records actions made through this server only |
| `/api/state`, `/api/competitors`, `/api/crawl`, `/api/reports*`, `/api/test/*` | **local** pipeline | the backend has no monitoring/report endpoints yet (see below) |

### Crawling with a backend
The monitoring pipeline stays in Node, but *how pages are fetched* changes: competitor sites are crawled with the
backend's `POST /v1/web/crawl` using the user's own API key, so they get real credit charging (1 per page), `robots.txt`
handling and SSRF protection, and the backend worker does the fetching. The app's own demo pages (`/test/...`) are never
sent to the backend (it blocks local addresses by design); they stay local and are not charged.

Errors from the backend show on the competitor card: `blocked by the site's robots.txt`,
`private/internal addresses are blocked`, `insufficient credits`.

## What the backend still needs

Done in the backend (no UI work left): the whole `/api/*` contract, signup name/plan, plan changes, key rotation, the admin list with plan/credits, plan edits, reset credits/password, audit log, scrape `description`, 403 for disabled accounts, a login rate limit that clears on success, scheduled crawls that skip suspended owners.

Fixed while integrating (in `backend/`): `/api/auth/login` and `/api/auth/signup` answered **500** for a NUL byte in the email or password (now 400), and the `/api` login limiter counted **successful** logins, so 10 good logins in 5 minutes locked a user out (it now clears on success). Tests added in `backend/tests/integration/test_ui_api.py`. `/theme.js` was added to the backend's page list (the light/dark toggle script).

Still open:

1. **Production readiness** (milestone M8): no public database port, secrets handling, rate limits stored in the database, backups, CI and deployment.
2. **Magento permissions for price changes**: the integration token needs *Catalog → Inventory → Products* and, on 2.4.4+, *Allow OAuth Access Tokens to be used as standalone Bearer tokens = Yes*. Until then the preview falls back to the last synced price with a warning and Apply answers Magento's own 401 (the change is kept as `failed`). The end-to-end tests cover this path with a stand-in store.
3. **`APP_ENCRYPTION_KEY`** must be set before a store token can be saved. Without it the backend now answers `503 {"error": "Storing a store token isn't set up on this server: ..."}` instead of a bare 500 (fixed here, with a test).
4. **Behind a proxy the backend must trust `X-Forwarded-For`** for its login limiter (uvicorn `--forwarded-allow-ips`; the default only trusts 127.0.0.1). Otherwise every user shares the proxy's IP.
5. **Product images** are not shown: the pages' CSP is `img-src 'self' data:`. Allow `https:` in both `pages.py` and `server.js` if you want thumbnails.
6. **The backend's own test-suite default** points at Postgres on :5433 (the compose file's published port); set `DATABASE_URL` otherwise. One of its tests (`test_add_competitor_rules`) needs DNS for example.com.

## Verifying it

* `npm run test:native`: 161 checks against the real backend through native mode: accounts, real product crawling of a stand-in shop, settings, product search, change detection, reports, **our store** (connect, sync, pause, token), **comparison** with currency rates, **price changes** (preview, apply, revert, guardrails, a refusing Magento) and the **agency approval flow**, admin and `/v1`.
* `npm run test:browser`: Playwright tests of the landing page (30 checks), the dashboard (23) and the comparison / price / admin screens (30).
* `npm test`: unit tests, including the integration layer against an in-process fake of the backend's contract.
* `npm run test:auth`: login/logout checks (58 standalone, 61 against the real backend: cookies, server-side session revocation,
  replay of old cookies, multiple devices, suspension, rate limiting, input handling). Works in both modes.
* `npm run test:integration`: end-to-end against a **real** backend (see `integration/README.md`): 53 checks across
  auth, sessions, real crawls through the worker, credits, change detection, robots.txt/SSRF errors, the `/v1` proxy,
  and the admin panel.
