# RivalWatch backend

Python 3.12 · FastAPI · Postgres 16 · Scrapling (fetching + parsing) · headless Chromium in the worker image (pages built by JavaScript) ·
LangGraph + Gemini (crawl agent).

## Run everything in Docker

All commands from `RivalWatch/backend`. This machine's Docker Desktop is off, so commands use the system daemon
(`--context default`); run `docker context use default` once to drop the flag.

```bash
cp .env.example .env                                  # set JWT_SECRET, APP_ENCRYPTION_KEY, GEMINI_API_KEY
docker --context default compose up -d --build        # db, migrate, api, worker, scheduler
```

| What | Where |
|---|---|
| Swagger (try every API) | http://localhost:8031/docs |
| ReDoc | http://localhost:8031/redoc |
| Health | http://localhost:8031/health |
| RivalWatch UI (served by the API) | http://localhost:8031/ · /login · /app · /admin |
| Postgres (DBeaver/psql) | localhost:5433, db `competitor`, user `postgres`, password from `POSTGRES_PASSWORD` |

Change the API port with `API_PORT` in `.env`.

### Is it running, and how?
```bash
docker --context default compose ps                    # each service, its state and ports
docker --context default compose logs -f api           # API requests and errors
docker --context default compose logs -f worker        # crawls as they run
docker --context default compose logs -f scheduler     # "queued N crawl(s)" every time a site's cron is due
curl -s localhost:8031/health                          # {"ok":true}
docker --context default compose exec db psql -U postgres -d competitor -c \
  "select type, status, payload, error, created_at from jobs order by id desc limit 10"   # the job queue
```
- **api**: FastAPI on port 8000 inside the container, published on 8031. Serves `/user/*`, `/admin/*`, `/v1/*` (crawl
  API, `x-api-key`), `/api/*` (the UI's own API, cookie session) and the UI pages.
- **worker**: runs queued jobs one at a time: `crawl_site` (the LangGraph crawl agent) and `web_crawl` (`/v1` jobs).
- **scheduler**: every minute, queues a crawl for each active competitor whose cron time has come (UTC).
- **migrate**: runs once at start-up (Alembic + every `site_<id>` schema), then exits.

### Run the crawler by hand inside Docker
```bash
docker --context default compose exec worker python scripts/try_crawl.py https://www.missoma.com/ --max-products 20 --twice
docker --context default compose exec worker python scripts/try_crawl.py https://www.indriya.com/ --max-products 8 --pages 60
docker --context default compose exec worker python scripts/try_crawl.py https://www.missoma.com/ --no-ai --keep
docker --context default compose exec worker python scripts/try_crawl.py --page https://www.missoma.com/products/<handle>
```
Run it in the **worker** container: it's the image with Chromium (the `app` image used by api/scheduler/migrate has no
browser, which keeps it small). `--keep` leaves the data in schema `site_<id>` for DBeaver.

### Create an admin (admins are only created in the database)
```bash
docker --context default compose exec api python scripts/create_admin.py admin@example.com 'a-long-password' \
  | docker --context default compose exec -T db psql -U postgres -d competitor
```

## Our own store (Magento)
`PUT /api/store {url, storeCode?, token?}` connects a workspace's Magento store. Its catalog is read through Magento's
GraphQL API (public data needs no token; a token, if given, is stored encrypted with `APP_ENCRYPTION_KEY`), synced on
the store's own cron by the scheduler (`sync_store` jobs), and stored in its own `site_<id>` schema with the same change
history as competitors. louped.btdemo.biz: 1,462 products in ~17 s.

## Crawl settings
Server defaults come from `.env`; each competitor can override them (`PATCH /api/competitors/{id}` with
`crawlSettings`, or the `crawl_settings` column).

| Setting | Default | Meaning |
|---|---|---|
| `maxProducts` | 200 (`DEFAULT_MAX_PRODUCTS`) | products collected per run (cap `MAX_PRODUCTS_CAP`, 5000) |
| `pageBudget` | 400 (`PAGE_BUDGET`) | pages fetched per run, every kind included (1 credit each) |
| `categories` | whole site | menu categories to stay inside (`GET /api/competitors/{id}/categories` lists them) |
| `sort` | `relevance` | which products fill the limit first: `relevance`, `newest`, `price_asc`, `price_desc`, `discount` |
| `cron` | `0 2 * * *` | when the scheduler crawls it (UTC) |
| `crawlSettings.delay_sec` | 0.5 (`CRAWL_DELAY_SEC`) | pause between requests to the site (robots.txt Crawl-delay wins if larger) |
| `crawlSettings.time_limit_sec` | 600 (`CRAWL_TIME_LIMIT_SEC`) | a run stops (status partial) after this long |
| `crawlSettings.browser` | `auto` (`BROWSER`) | `auto` renders pages built by JavaScript in headless Chromium; `off` never does |
| `crawlSettings.browser_pages` | 30 (`BROWSER_PAGES`) | most pages rendered per run (slow: ~5-10 s each) |
| `crawlSettings.product_meta` | true (`PRODUCT_PAGE_META`) | feed sites: open each product page once for its meta tags |
| `crawlSettings.ai_extract` | true (`AI_EXTRACT`) | pages with no structured data and no clear price: Gemini reads them (checked against the page) |
| `crawlSettings.ai_extract_cap` | 30 (`AI_EXTRACT_CAP`) | most AI-read pages per run |

How a product page is read, cheapest first: the platform's feed (Shopify/WooCommerce) → JSON-LD → OpenGraph →
the page's own heading and price → rendered in the browser (if the page is built by JavaScript) → Gemini.
Template values (`{{name}}`) are never stored, and sitemaps that date every URL "today" are ignored.

## Local development (without Docker for the app)
```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
docker --context default compose up -d db
export DATABASE_URL=postgresql://postgres:postgres@localhost:5433/competitor BROWSER_CHANNEL=chrome
.venv/bin/alembic upgrade head && .venv/bin/python -m competitor_moves.db.site_schema
.venv/bin/uvicorn competitor_moves.api.main:app --port 8031 --reload
.venv/bin/python -m competitor_moves.workers.worker
.venv/bin/python -m competitor_moves.workers.scheduler
```
Tests: `.venv/bin/python -m pytest` (needs the dev database; never calls the real Gemini) · lint: `.venv/bin/ruff check .`

## Data layout
`public`: users, sessions, projects (one workspace per user), sites (competitors and their settings), moves, jobs,
price_changes, audit_log.
Each website gets its own schema `site_<id>`: `products` (with source page, path, currency and the page's meta tags),
`price_history`, `crawl_runs`, `pages` and `history` (every change ever detected, linked to its crawl run, its product
and, for changes a person made, the user).
