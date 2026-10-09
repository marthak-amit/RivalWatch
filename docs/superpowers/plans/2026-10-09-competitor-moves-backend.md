# Competitor Moves Backend Implementation Plan

> **SUPERSEDED (2026-10-09):** replaced by `/home/devarshi/.claude/plans/cuddly-jumping-turtle.md` (milestones M0–M8). Kept for history only.


> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A backend where a user enters their store URL plus 3–4 competitor URLs and gets back a feed of what each competitor did this week, with evidence.

**Architecture:** FastAPI serves a REST API and a Python worker runs jobs from a Postgres table. The worker crawls with Scrapling (feeds, sitemaps and JSON-LD first), writes products and price snapshots, turns differences into events, groups events into moves, and asks Gemini only to word them. Phase-level plan: each task lists files, interfaces and a check. Write the failing test first inside each task.

**Tech Stack:** Python 3.12, FastAPI, Scrapling, psycopg 3 (raw SQL), `google-genai`, Postgres 16 (`pg_trgm` not needed now), Docker Compose.

**Spec:** `docs/superpowers/specs/2026-10-09-competitor-moves-backend-design.md`

## Global Constraints

- The only external credential is `GEMINI_API_KEY`. No other paid service, proxy or API. Auth is our own bearer-token login (spec §15). `MAGENTO_*` is optional and ours.
- Competitors are entered by the user. There is no AI competitor discovery.
- Respect robots.txt. On persistent 403/429/captcha mark the site `blocked`. No stealth bypass.
- `PAGE_BUDGET=400` per site per run, `SEARCH_PAGE_BUDGET=30`, `AI_EXTRACT_CAP=30`, 2 concurrent requests per domain, 20s timeout, 2 retries.
- Numbers come from code. Every number in an AI headline must appear in that move's `size`, otherwise use the template headline.
- Prices extracted by AI (`extracted_by='ai'`) are tagged and never drive a "discounting" move.
- Model name comes from `GEMINI_MODEL` (docs list `gemini-3.5-flash-lite` as fastest/cheapest and `gemini-3.8-flash` as newest; confirm in P0).
- Worker is synchronous (threads for fetches) to avoid async and library surprises.

## Review Focus

- A site with 0 products or no sitemap: crawl ends `ok` with 0 products, no crash (test in P1 T4).
- Sitemap index with `.gz` files and a 20M-product site: only recent files are read, budget respected (P1 T3).
- Sampled first crawl must not report old products as new (P2 T6).
- A product with no price or an `AggregateOffer`: no crash, no fake price event (P1 T4).
- Gemini returns invalid JSON or is down: moves still appear with template headlines (P3 T8).

## Phase 0 (hours 0–1): Align and scaffold

### Task 1: Repo, Docker, schema, stub API
**Files:** Create `backend/docker-compose.yml`, `backend/Dockerfile`, `backend/schema.sql`, `backend/.env.example`, `backend/app/main.py`, `backend/app/db.py`, `backend/fixtures/*.json`.
**Interfaces:** Produces the API routes in spec §9 returning fixture data, and the Move JSON from spec §5.3.
- [ ] Write `schema.sql` from spec §8 (minus `discovery_reason`). Mount it in the db container's `docker-entrypoint-initdb.d`.
- [ ] `main.py`: one FastAPI app, one dependency checking `X-API-Token == API_TOKEN`, every route returns its `fixtures/<name>.json`.
- [ ] Check: `docker compose up`, then `curl -H "X-API-Token: x" localhost:8000/projects/1/moves` returns the fixture; no token returns 401.
- [ ] Send the frontend person `localhost:8000/docs` and the Move JSON.

### Task 2: Hour-0 spikes (AI dev, throwaway scripts in `scratch/`)
- [ ] Pick the demo store. It must have a sitemap with `lastmod`, JSON-LD `Product` and robots allowing product pages. Shopify preferred. Pick 3–4 competitors the same way. Save their HTML into `backend/fixtures/html/`.
- [ ] Confirm the install command from the Scrapling README and that `Fetcher.get(url).status` and `Selector(html, url=u).css('script[type="application/ld+json"]::text').getall()` work.
- [ ] Confirm one Gemini call returns JSON. Docs show `client.interactions.create(model=…, input=…, response_format={"type":"text","mime_type":"application/json","schema": Model.model_json_schema()})` then `Model.model_validate_json(i.output_text)`. If `generate_content` is easier, use it. Note whichever works in `ai.py`'s docstring.

## Phase 0b: Auth and roles (added, spec §15)
### Task 1b: Login, logout, roles (`app/auth.py`)
- [x] DONE: `tests/test_auth.py` (7 tests) and `app/auth.py`. JWT + revocation on logout, separate `/user` and `/admin` logins, user signup, admin user management, admins inserted by hand.

## Phase 1 (hours 1–4): Crawl and store (Crawl/API dev), AI helpers (AI dev)

### Task 3: Fetch, robots, platform, sitemap (`app/crawl.py`)
**Interfaces:** Produces `fetch(url) -> Response|None`, `allowed(url) -> bool`, `detect_platform(home_html, domain) -> str`, `recent_urls(domain, days=7, cap=...) -> list[tuple[str, date|None]]`, `est_total(domain) -> int`.
- [ ] Test `test_sitemap.py`: an index with two child sitemaps, one with old `lastmod` (must not be fetched), one `.gz`; assert the right URLs and that the old child was skipped.
- [ ] Implement with `urllib.robotparser`, `Fetcher.get`, `xml.etree`, `gzip`.

### Task 4: Extraction (`app/extract.py`)
**Interfaces:** Produces `extract(html, url) -> dict|None` with keys `title, sku, gtin, brand, category, image, price, compare_at_price, currency, in_stock, source_date, extracted_by`, and `from_shopify(product_json) -> list[dict]`.
- [ ] Test `test_extract.py` on saved HTML: JSON-LD list, `@graph`, `offers` as object/list/`AggregateOffer`, missing price (returns price `None`), Shopify product JSON with `compare_at_price`.
- [ ] Implement feed first, then JSON-LD, then OpenGraph. The AI fallback is wired in P3.

### Task 5: Job queue and worker (`app/worker.py`)
**Interfaces:** Produces `enqueue(type, payload, run_after=None)`, `claim() -> job|None`, handlers `crawl_site(site_id)`, `analyze(project_id)`, `search(search_id)`.
- [ ] Claim query: `UPDATE jobs SET status='running', attempts=attempts+1 WHERE id=(SELECT id FROM jobs WHERE status='queued' AND run_after<=now() ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *`.
- [ ] Failed job: re-queue until 3 attempts, then `failed` with the error stored. Test that the job is claimed once by two concurrent claimers.
- [ ] `POST /projects` creates the project, our site, the competitor sites, and one `crawl_site` job per site.
- [ ] `crawl_site`: platform detect, then budget order from spec §4.3 (home/menu → feed → sitemap → known products → sample), writes `products`, `snapshots` (only when hash changes), and a `crawls` row with `pages_fetched`, `est_total_products`.
- [ ] Check: demo URL plus competitors in, `products` rows appear, `crawls.status='ok'`.

## Phase 2 (hours 4–6): Events and the thin path

### Task 6: Events (`app/diff.py`)
**Interfaces:** Produces `write_snapshot(conn, product, row, crawl) -> list[event]` (called by crawl and search) and `home_events(conn, site, old_home, new_home)`.
- [ ] Test `test_diff.py` for: sampled site first sight is not `new_product`; `source_date` inside 7 days is `new_product`; `compare_at_price > price` gives `on_sale`; price change gives drop/rise; stock flip; homepage promo text change gives `promo_started`; menu link added gives `new_category`.
- [ ] Implement per spec §5.1.

### Task 7: Thin path moves and API
- [ ] `group_moves(conn, project_id, days) -> list[Move]` for `launched_products` only, with template headline. `GET /projects/{id}/moves` reads from the `moves` table instead of the fixture.
- [ ] **H6 checkpoint:** the demo URL in, 3 competitors crawled, a `launched_products` move through the real API. Then set the demo project's `crawl_interval_hours` to 1.

## Phase 3 (hours 6–9): Remaining moves, AI wording, search

### Task 8: Gemini layer (`app/ai.py`)
**Interfaces:** Produces `extract_product(page_text) -> dict|None`, `write_moves(facts) -> dict`, `plan_search(query, nav) -> dict`, `answer_search(query, results) -> dict`. Each validates against a Pydantic model and returns `None` on any error.
- [ ] Test `test_moves.py`: grouping, and a headline with a number not in `size` is replaced by the template.
- [ ] Wire `extract_product` into `extract` (cap 30 per crawl). Add moves `new_category`, `discounting`, `price_increase`, `stock_out`, and `notable` (from `page_changed`). `analyze` deletes and re-inserts the window's moves, then stores `weekly_summary`.
- [ ] Check with `GEMINI_API_KEY` unset: moves still return with template headlines.

### Task 9: Search with tracking (`app/search.py`)
- [ ] Routes in order: feed plus keyword filter, site search only if robots allows, planner-picked category pages, sitemap slug match (5 files max). Results go to `products`, `snapshots`, `search_results`. `plan_search` output is accepted only for `category_urls` found in `crawls.nav`.
- [ ] Scheduler tick in `worker.py`: every minute enqueue a refresh for projects past `crawl_interval_hours`, including tracked searches.
- [ ] **H9 checkpoint:** all move types and search work through the real API.

### Task 9b: Our catalog, SKU comparison, price changes (added, spec §15)
- [ ] `test_compare.py`: SKU normalization (`AB-12` matches `ab 12`), comparison rows and gap %, price change writes the audit row and rejects price <= 0 or an unknown SKU.
- [ ] Implement `app/compare.py` (`norm_sku`, `comparison(conn, project_id)`) and `app/pricing.py` (`apply_price(conn, user, project_id, sku, new_price)` with optional Magento push).

## Phase 4 (hours 9–11): Replace stubs, harden

### Task 10: Real routes, run log, summary
- [ ] Replace the remaining fixtures: `GET /projects/{id}`, `/summary`, `/events`, `/sites/{sid}/products`, `/crawls`, competitor add/remove, `POST /refresh`, searches.
- [ ] `smoke.py`: POST the demo URL, poll until jobs finish, assert moves not empty.
- [ ] Tune prompts on the demo data. **H11: feature freeze.**

## Phase 5 (hours 11–13): Integrate and ship

### Task 11: Integration and deploy
- [ ] Connect with the frontend, run the demo path 3 times, fix only bugs.
- [ ] `pg_dump` of a known good state plus a restore command in `README.md`.
- [ ] Deploy `docker compose up -d` on the server with `.env` filled in.

## Phase 6 (hours 13–14): Buffer
- [ ] Fixes only, record the backup demo.

**Cut list (in order):** site-search route, random sample, AI fallback extraction, stock events, search tracking. Keep `notable` moves.
