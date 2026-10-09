# Competitor Moves Agent: Backend Design

> **STATUS (2026-10-09): partly superseded.** The architecture, data model, crawl flow, file layout and milestones are now defined in `/home/devarshi/.claude/plans/cuddly-jumping-turtle.md` (LangGraph crawl agent, one schema per site, daily cron, Magento, price changes, no Langfuse, no AI competitor discovery, no product search).
> Still valid from this document: the goal (§1), event rules (§5.1), move grouping and the number guardrail (§5.2), the Move JSON (§5.3), crawl politeness and page budget (§4), and the auth addendum (§15).
> Superseded: §3 architecture, §6 AI layer, §7 search, §8 data model, §9 API paths, §10 files, §12 schedule.


Date: 2026-10-09 · Build window: 14 hours · Scope: backend only (frontend is built by another person against this API)

## 1. Goal

> A store owner types their URL and, within minutes, sees **what the 3–4 competitors they entered did this week**, in plain sentences with proof, without anyone checking sites by hand.

Every feature serves this sentence. Anything that does not is out.

**Success for the demo:**
- The pre-picked demo store and 3 hand-entered competitors are crawled and show real moves.
- At least 5 real moves across them, each with evidence (product links, before/after values).
- Each move answers what happened, how big it was, when, and whether it touches the user's own categories.
- One search ("waterproof jackets under $150") returns a comparison across sites and can be tracked.
- No fabricated data. History comes from real crawls and from dates the sites publish themselves.

## 2. Scope

**In:**
- The user enters their store URL and 3–4 competitor URLs by hand (no AI discovery), then a budgeted crawl of every site.
- Change events, grouped into **moves**, with an AI write-up.
- Search with tracking.
- A crawl log, token auth, Docker Compose.

**Out (future scope):**
- Login and roles, Magento admin menu, mobile app, alerts and digests.
- AI competitor discovery (the user types competitors in), fuzzy product matching, ranked opportunities.
- Any paid service other than the Gemini API key (no proxies, no SocialCrawl).
- Social signals (SocialCrawl API), multiple clients on one system, bypassing anti-bot protection.

**Demo input:** one real store picked in hour 0–1 and checked for data quality (sitemap with `lastmod`, JSON-LD `Product`, robots allows product pages; Shopify competitors preferred). Live URLs from judges are best-effort.

## 3. Architecture

```
POST /projects {url, competitors[]}
   │
   ▼
jobs table (Postgres) ◄── worker (same image, different command)
   ├─ crawl_site(ours)        → products, snapshots, events, home/nav snapshot
   ├─ crawl_site(competitor×N)→ products, snapshots, events, home/nav snapshot
   ├─ analyze                 → events → moves (+ AI headline/why/summary)
   └─ search                  → LLM plan → focused crawl → comparison answer
                                         │
frontend (other dev) ◄── FastAPI ◄───────┘   frontend polls; no websockets
```

- **Stack:** Python 3.12, FastAPI, Scrapling, psycopg 3 (raw SQL, no ORM), `google-genai`, Postgres 16 with `pg_trgm`.
- **Docker Compose:** `db`, `api`, `worker`. One image; `api` runs uvicorn and `worker` runs `python -m app.worker`.
- **Queue:** the `jobs` table, claimed with `FOR UPDATE SKIP LOCKED`. No Redis or Celery. To scale, run more worker containers.
- **Scheduler:** the worker loop checks once a minute for projects past `crawl_interval_hours` and enqueues a refresh (crawl all sites, re-run tracked searches, analyze).
- **Job chaining:**
  - `POST /projects` enqueues one `crawl_site` per site (ours plus each competitor).
  - When a crawl or search job finishes and no crawl or search jobs for that project are still queued or running, it enqueues `analyze`. A unique partial index keeps it to one queued `analyze` per project.
- **Isolation:** each site is its own job. A blocked site is marked `blocked` and the project carries on.
- **Auth:** an `X-API-Token` header matched against the `API_TOKEN` env value. Required because a public server would otherwise spend our Gemini key.

## 4. Crawling

### 4.1 Rules
- Respect robots.txt (stdlib `urllib.robotparser`). At most 2 concurrent requests per domain with a 0.5s delay. A fixed User-Agent.
- Use the plain HTTP fetcher first. Use the Scrapling browser fetcher only when the page looks JS-rendered (no JSON-LD and little body text). Each browser fetch counts toward the budget.
- On persistent 403/429/captcha: stop and mark the site `blocked`. **No stealth bypass.**
- Each request has a 20s timeout and 2 retries with backoff. Each site crawl has a 10-minute cap, after which its status is `partial`.

### 4.2 Platform detection (homepage + one probe)
- **Shopify:** `/products.json` returns `{"products": …}`, or the HTML contains `cdn.shopify.com`.
- **WooCommerce:** `/wp-json/wc/store/v1/products` returns a JSON array.
- **Magento:** the HTML contains `Magento_` or `mage/`.
- Anything else is `generic`.

### 4.3 Page budget (the answer to the 20M-product problem)
Every site gets `PAGE_BUDGET` (default 400) fetches per run, whatever its size. The budget is filled in this order:
1. **Home page and menu** (1–2 requests), always. Stored in `crawls.home` and `crawls.nav`.
2. **Platform feed:**
   - Shopify `/products.json?limit=250&page=n`: 250 products per request, with `created_at`, `compare_at_price` and variants.
   - Shopify `/collections.json`: `published_at`.
   - WooCommerce Store API.
3. **Sitemap URLs changed in the last 7 days:**
   - Read robots.txt `Sitemap:` lines, falling back to `/sitemap.xml` or `/sitemap_index.xml`. Handle `.gz` files.
   - Skip sitemap files whose own `lastmod` is older than 7 days, without reading them. Then take URLs with `lastmod` in the last 7 days.
4. **Products from tracked searches.**
5. **Products seen before**, least recently checked first. These are where price and stock changes come from.
6. **Random sample** of unseen sitemap URLs, to fill the rest of the budget.

`crawls.est_total_products` is the URL count from the product sitemaps we read. For large indexes it's the number of files × 50,000, labelled with `~`. The API returns it so the frontend can say "sampled 400 of ~20M".

### 4.4 Extraction (stop at the first source that works)
1. Platform feed JSON.
2. JSON-LD `Product`: handles `@graph`, lists, `offers` as an object, a list or an `AggregateOffer` (`lowPrice`), and `priceSpecification`.
3. OpenGraph `product:price:amount`.
4. Gemini fallback on page text trimmed to about 8k characters, at most `AI_EXTRACT_CAP` (30) per crawl, with `extracted_by='ai'` set.

Fields: title, sku, gtin, brand, category (from the breadcrumb or URL path), image, price, compare_at_price, currency, in_stock, source_date (Shopify `created_at` or sitemap `lastmod`).

A snapshot row is written only when the hash of (price, compare_at_price, in_stock, title) changes.

## 5. Events and moves

### 5.1 Events (computed with ordinary code, no AI)
| Event | Rule |
|---|---|
| `new_product` | `source_date` is within the 7-day window. If the site publishes no dates: only when the previous crawl had full coverage (`est_total_products <= PAGE_BUDGET`) and the URL was not in it. A first-seen product on a sampled site is **not** new, because sampling would make old products look new. `occurred_at` = source_date if known, else detection time. |
| `new_category` | Shopify collection `published_at` within 7 days, or a category link in the menu that was not there on the previous crawl, or (generic first crawl) a category with at least 3 products found, all of them `new_product`. |
| `on_sale` | `compare_at_price > price` becomes true, including the first time a product is seen. |
| `price_drop` / `price_rise` | Comparing a product's snapshot with its previous one (never on first sight). |
| `out_of_stock` / `back_in_stock` | `in_stock` flips. |
| `promo_started` / `promo_ended` | Homepage text matches the promo pattern (`\d+% off`, sale, discount, coupon, clearance, free shipping, BOGO) and differs from the last crawl. |
| `page_changed` | The homepage text or menu changed since the last crawl. Its before/after is fed to the "notable" write-up. |

### 5.2 Moves
- **Grouping:** events from the last 7 days are grouped by (site, move type, category). Each group becomes one move.
- **Our own site:** gets moves too ("what we did this week"), but no "why it matters" line.

| Move type | Built from |
|---|---|
| `launched_products` | `new_product` |
| `new_category` | `new_category` |
| `discounting` | `on_sale`, `price_drop`, `promo_started` |
| `price_increase` | `price_rise` |
| `stock_out` | `out_of_stock` |
| `notable` (`ai_observed=true`) | Gemini's free-form reading of the `page_changed` before/after, e.g. "now offers free returns", "new Black Friday early-access page". This keeps the feed from being limited to the rules we wrote. |

- **Size:** `{count, avg_pct_off?, categories?}`, all calculated with ordinary code.
- **AI write-up:** one Gemini call per site per run (§6) writes `headline`, `why_it_matters` and a 2-line weekly `summary` for the site.
- **Number guardrail:** every number in an AI headline must appear in that move's `size`. Otherwise the headline is replaced by a template such as `"Launched {count} products in {category}"`.
- **Regeneration:** `analyze` replaces the project's moves for the current 7-day window each run, by deleting and re-inserting them.

### 5.3 Move JSON (locked at hour 0, the contract with the frontend)
```json
{
  "id": 41,
  "site": {"id": 3, "domain": "competitor-b.com", "role": "competitor"},
  "type": "discounting",
  "category": "Outdoor jackets",
  "headline": "Discounting 38 outdoor jackets, 22% off on average",
  "why_it_matters": "You sell 21 products in this category.",
  "size": {"count": 38, "avg_pct_off": 22},
  "evidence": [
    {"event_id": 901, "type": "price_drop", "product_url": "https://…", "title": "…",
     "before": {"price": 120.0}, "after": {"price": 89.0}, "occurred_at": "2026-10-07T10:00:00Z"}
  ],
  "ai_observed": false,
  "window": {"from": "2026-10-03", "to": "2026-10-09"}
}
```
`evidence` holds at most 5 items in list responses. The full set comes from `GET /events?move_id=`.

## 6. AI layer (all in `app/ai.py`; model name from `GEMINI_MODEL`)

| Function | When | Output / guardrail |
|---|---|---|
| `extract_product` | fallback only, at most 30 per crawl | Strict JSON. Output tagged `extracted_by='ai'`. |
| `plan_search` | once per site per search | `{terms[], category_urls[], filters{min_price,max_price,brand,keywords[]}}`. A `category_url` is accepted only if it exists in that site's stored menu. |
| `answer_search` | once per search | `{text, cited_product_ids[]}`. Cited IDs that aren't in the results are removed. |
| `write_moves` | once per site per run | Input: the grouped move facts, the before/after of `page_changed`, and our category list. Output: `{moves:[{key, headline, why_it_matters}], notable:[{headline, before, after}], summary}`. The number guardrail from §5.2 is applied. |

If Gemini fails, moves keep their template headlines and the summary is marked stale. The data still shows.

## 7. Search with tracking

1. `POST /projects/{id}/searches {query, site_ids?, tracked}` enqueues a `search` job.
2. `plan_search` runs once per selected site.
3. The executor takes the first route the site allows, with up to `SEARCH_PAGE_BUDGET` (30) pages per site:
   - Feed + keyword filter (Shopify/WooCommerce).
   - The site's own search URL (from the JSON-LD `WebSite.potentialAction` `SearchAction`, or the homepage search form), **only if robots.txt allows it**. Default Shopify (`/search`) and Magento (`/catalogsearch/`) robots.txt files block this.
   - Category pages chosen by the planner (product links, then product pages).
   - Sitemap URL slug keyword match, reading at most 5 sitemap files.
4. The same extractor runs and results go into `products` and `snapshots` as usual. Filters are applied, and the results are written to `search_results`.
5. `answer_search` writes the comparison across sites.
6. `tracked=true` searches are re-run on every scheduled refresh, so their products produce price, sale and stock events.

## 8. Data model (`schema.sql`)

```
projects(id, url, crawl_interval_hours int default 24, created_at)
sites(id, project_id, domain, role ours|competitor, status active|blocked|removed,
      platform shopify|woo|magento|generic, search_url_template,
      weekly_summary, summary_at, last_crawl_at)
crawls(id, site_id, kind full|search, status running|ok|partial|blocked|failed,
       pages_fetched, pages_failed, est_total_products, home jsonb, nav jsonb, error,
       started_at, finished_at)
products(id, site_id, url, title, sku, gtin, brand, category, image, source_date,
         extracted_by feed|jsonld|og|ai, first_seen_at, last_seen_at, last_checked_at,
         unique(site_id, url))
snapshots(id, product_id, crawl_id, price numeric, compare_at_price numeric, currency,
          in_stock bool, hash, fetched_at)
events(id, project_id, site_id, product_id null, type, category, before jsonb, after jsonb,
       occurred_at, detected_at)
moves(id, project_id, site_id, type, category, headline, why_it_matters, size jsonb,
      evidence_event_ids int[], ai_observed bool, window_from, window_to, created_at)
searches(id, project_id, query, site_ids int[], plan jsonb, tracked bool, status,
         answer jsonb, created_at, last_run_at)
search_results(search_id, product_id, site_id, rank)
jobs(id, type, payload jsonb, status queued|running|done|failed, run_after, attempts,
     error, created_at)
```

## 9. API (all routes require `X-API-Token`; OpenAPI at `/docs`)

```
POST   /projects {url, competitors[]}          → {id}; enqueues a crawl per site
GET    /projects                                → list
GET    /projects/{id}                           → sites[] with status, platform, product count,
                                                  est_total, last crawl; job progress counts
POST   /projects/{id}/competitors {domain}      → add a competitor and crawl it
DELETE /projects/{id}/competitors/{site_id}     → mark removed
POST   /projects/{id}/refresh                   → crawl all + re-run tracked searches + analyze
GET    /projects/{id}/moves?days=7&site_id=&type=
GET    /projects/{id}/summary                   → per-site weekly summary + move counts by type
GET    /projects/{id}/events?days=&site_id=&type=&move_id=
GET    /projects/{id}/sites/{sid}/products?new_days=&on_sale=&q=&page=
POST   /projects/{id}/searches {query, site_ids?, tracked} → {id}
GET    /searches/{id}                           → status, plan, results per site, answer
PATCH  /searches/{id} {tracked}
GET    /projects/{id}/crawls                    → run log and collection health
```

**By hour 1, every route returns stub data from `fixtures/*.json`** (same shapes), so the frontend is never blocked. Stubs are replaced route by route.

## 10. Files

```
backend/
  docker-compose.yml   Dockerfile   schema.sql   .env.example
  app/
    main.py      routes + pydantic response models + token check
    db.py        connection pool + SQL helpers
    worker.py    job loop, job chaining, scheduler tick
    crawl.py     robots, platform detect, sitemap reader, budget, fetch
    extract.py   feed / JSON-LD / OpenGraph / AI fallback
    diff.py      snapshot write → events (called by crawl + search); events → move groups
    search.py    plan execution routes
    ai.py        the four Gemini functions
  fixtures/      stub API responses + saved HTML from demo sites
  tests/
```

Env: `DATABASE_URL`, `GEMINI_API_KEY`, `GEMINI_MODEL`, `API_TOKEN`, `PAGE_BUDGET=400`, `SEARCH_PAGE_BUDGET=30`, `AI_EXTRACT_CAP=30`, `USER_AGENT`.

## 11. Errors and testing

**Errors:**
- Request and crawl timeouts as in §4.1.
- A job gets up to 3 attempts, then `failed` with the error stored; it is visible through `/crawls`.
- If Gemini is down, see §6.
- **Demo backup:** a `pg_dump` of a known good state plus a restore command, and a recorded demo video.

**Tests (one runnable check per piece of logic):**
- `test_extract.py`: real saved HTML from the demo sites (Shopify feed, JSON-LD with `@graph`, `AggregateOffer`), checking the expected fields.
- `test_sitemap.py`: index + `lastmod` filter + `.gz`.
- `test_diff.py`: two snapshot sets, checking the expected events, including "first crawl old product = no event".
- `test_moves.py`: grouping, and the number guardrail rejecting a headline with a made-up number.
- `smoke.py`: POST the demo URL, poll, and assert that moves are not empty.

## 12. Schedule (2 backend devs; with one dev the order is the same and the cut list starts earlier)

| Hours | Crawl/API dev | AI dev |
|---|---|---|
| 0–1 | Repo, Compose, `schema.sql`, stub routes from fixtures, **Move JSON locked** | **Pick the demo store and check its competitors**. Confirm the Gemini model name, JSON output call and Scrapling API names |
| 1–4 | Jobs + worker, robots, platform detection, sitemap reader, fetch, feed + JSON-LD extraction into products/snapshots | `extract_product` fallback, save HTML fixtures + `test_extract` |
| 4–6 | Diff into events, home/menu snapshot, budget order | Move grouping, `write_moves` + number guardrail, notable moves from `page_changed` |
| **H6 checkpoint** | Demo URL in → 3 competitors crawled → a `launched_products` move visible through the real API. **Then set the demo project's `crawl_interval_hours` to 1.** | |
| 6–9 | Search executor (all 4 routes), tracked re-run, `new_category` (`collections.json` / menu diff) | `plan_search`, `answer_search`, tune moves on demo data |
| **H9 checkpoint** | All move types + search through the real API | |
| 9–11 | Real routes replace stubs, crawl log, token auth, final Docker image | Prompt tuning, summary quality, `smoke.py` |
| **H11 feature freeze** | | |
| 11–13 | Connect with the frontend, demo path run 3 times, `pg_dump` backup, deploy to server | |
| 13–14 | Buffer, record the backup demo | |

**Cut list if behind (in order):** the site-search route, the random sample, AI fallback extraction, stock events. The `notable` move stays, because it is what keeps the feed from being scripted.

## 13. Demo path (about 3 minutes)
1. Type the demo store URL. Add 3 competitor URLs by hand.
2. The moves feed fills in: "B launched 14 products in Trail running", "C is discounting 38 jackets, 22% off on average", "A opened a new Kids category (you don't sell this)", "B now offers free returns" (AI-observed).
3. Click a move to see its evidence: product links, before → after.
4. Search "waterproof jackets under $150" to get a comparison across sites, then track it.
5. Close on future scope: social moves through SocialCrawl, a weekly email digest, product matching with price gaps, applying changes in Magento after approval.

## 14. Risks
| Risk | Handling |
|---|---|
| A site forbids or blocks crawling | robots.txt respected, no bypass, the site shows `blocked`. The demo set is checked in advance. |
| No price history on demo day | Day-one moves come from dates the sites publish (`created_at`, `lastmod`, `published_at`) and from `compare_at_price`. Hourly crawls from H6 add real diffs. Nothing is faked. |
| The AI invents numbers or products | Numbers are calculated with ordinary code. Headlines are checked against `size`, cited IDs are checked, and AI-extracted prices are tagged. |
| Library or model API names differ from this spec | Checked in hour 0 before building on them. |
| Live demo failure | `pg_dump` restore + recorded video. |

## 15. Addendum (2026-10-09): login, our own store, SKU comparison, price changes

Replaces the shared `X-API-Token` and moves "apply price change" from future scope into scope.

**Auth and roles (built)**
- Two sections with separate logins. `/user/*`: `POST /user/signup`, `/user/login`, `/user/logout`, `GET /user/me`. `/admin/*`: `POST /admin/login`, `/admin/logout`, `GET /admin/users`, `PATCH /admin/users/{id} {is_active}`, `DELETE /admin/users/{id}`.
- **Admins are never created through the API.** Insert them by hand in the DB (`python -m app.auth EMAIL PASSWORD` prints the INSERT with a hashed password). `/admin/login` accepts only admins and `/user/login` accepts only users.
- Proper JWT (HS256, PyJWT): claims `sub`, `role`, `jti`, `iat`, `exp`. TTL is 7 days for users and 12 hours for admins. The frontend sends `Authorization: Bearer <token>`.
- **Sessions (migration 0003, replaces `revoked_tokens`):** every login (and signup) creates a `sessions` row keyed by the token's `jti`, with `started_at`, `last_seen_at` (updated at most once a minute), `expires_at`, `ended_at`, `end_reason` (`logout | expired | idle | disabled`), IP and user agent. A token is accepted only while its session is open, not expired and not idle. The idle timeout is 30 minutes for admins and 8 hours for users (`IDLE_TIMEOUT_ADMIN_MIN` / `IDLE_TIMEOUT_USER_MIN`). Session length = `last_seen_at - started_at`. Disabling a user ends all their sessions. Role and `is_active` are still read from the DB on every request.
- **Session time in the API:** login and signup return `expires_at` and `idle_timeout_sec`. `GET /user/me` returns `session {started_at, last_seen_at, expires_at, expires_in_sec, idle_timeout_sec}`. `GET /admin/users` adds `active_now`, `session_count`, `last_login_at`, `last_session_seconds` and `total_session_seconds`. `GET /admin/users/{id}/sessions` lists the last 50 sessions with duration and end reason (the token id is never returned).
- Passwords use `hashlib.scrypt` (stdlib). Unknown email and wrong password cost the same time and give the same 401.
- Admins can list, deactivate and delete users, but not other admins. Read routes under `/user/*` accept any logged-in account, including admins. All write routes (catalog, competitors, price changes) go under `/admin/*` with `require_admin`.
- Not built yet: login rate limiting, password reset, email verification.

**Our own store**
- A project's `ours` site is a controlled catalog. Admin uploads or syncs it: `POST /admin/projects/{id}/catalog` takes a CSV or JSON list of `{sku, title, price, cost?}`. It is also read by the crawler when the platform allows. The catalog is stored as `products` rows on the `ours` site, so everything else (moves, events) works unchanged.
- Optional push to the real store: if `MAGENTO_BASE_URL` and `MAGENTO_TOKEN` are set, applying a price also calls `PUT /rest/V1/products/{sku}` with `{"product": {"price": X}}`. Not set means our catalog is updated only. The push result is recorded.

**SKU comparison**
- Match key: normalized SKU (lowercase, trimmed, spaces and dashes removed), then GTIN. Exact match only, no fuzzy matching.
- `GET /user/projects/{id}/comparison` returns one row per matched SKU: our price and each competitor's price, gap %, who is cheapest, and the competitor's `on_sale` flag.

**Price changes**
- `POST /admin/price-changes {project_id, sku, new_price}` validates `new_price > 0` and that the SKU exists on our site, updates our price, writes a `price_changes` audit row `(sku, old_price, new_price, user_id, applied_at, pushed, push_error)`, and optionally pushes to Magento.
- `GET /admin/price-changes` lists the audit log.
- Guardrails now: admin only, price must be positive, full audit log. A price floor/ceiling, preview step and rollback stay in future scope.

New tables: `users(id, email unique, password_hash, role, is_active)`, `sessions(jti, user_id, role, started_at, last_seen_at, expires_at, ended_at, end_reason, ip, user_agent)`, `price_changes(...)`. `products` gets `price_source` so a manual price change on our site is not overwritten by a crawl.
