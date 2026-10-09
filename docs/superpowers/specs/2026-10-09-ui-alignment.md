# Aligning the Python backend with the RivalWatch UI

Date: 2026-10-09. Decisions confirmed by the user: (1) the backend serves the UI's existing `/api/*` contract and its `public/` pages, so the UI works unchanged; (2) plans, crawl credits and the per-user API key stay as a mock, like the UI; (3) competitor comparison uses category and attributes, plus exact SKU/barcode only when both sites share one; (4) backend lives in `RivalWatch/backend/` and the user pushes it manually.

Read-only inputs: UI repo `marthak-amit/RivalWatch` (Node, `server.js` + `lib/` + `public/`), our Magento store `https://louped.btdemo.biz` (jewellery, 1,462 products, public GraphQL works; the integration token still lacks `Magento_Catalog::products`).

## 1. Model change: one workspace per user
The UI gives every user an isolated workspace with their own competitors, plan and credits. So:
- Each user owns one `projects` row (`owner_user_id`, unique). The project has one `ours` site (the Magento store, shared config) and up to `plan.competitors` competitor sites that the user adds from the dashboard.
- This replaces the earlier idea that only an admin manages sites. The **admin** is now the platform admin: manages users, plans, credits and the audit log.
- Every site still gets its own `site_<id>` schema. If two users add the same domain they get two site schemas (simple, isolated; dedupe later if storage matters).
- Admins are still created only by hand in the DB. The UI admin panel's "role" choices for creating or promoting admins are disabled server-side (the API answers 403 for `role: admin`), which the UI person should hide in the panel.

Migration `0002` adds to `users`: `name`, `plan`, `api_key` (unique), `credits`, `last_login_at`, and maps the UI's `status` (`active|suspended`) to `is_active`. It adds `projects.owner_user_id`, `audit_log(ts, actor_email, action, target_email, detail)`, and `crawl_log` per site already lives in `crawl_runs`.

## 2. Routes to implement (same paths and shapes as `server.js`)
Session: HttpOnly cookie `rw_session` holding our JWT, backed by the same `sessions` table as the bearer routes (session time and idle timeout apply the same way; the admin users table can show `lastLoginAt`, active now and total time) (`SameSite=Lax`, `Secure` behind HTTPS). Non-GET requests must send `application/json` (else 415), as the UI does. Login/signup are rate limited (10 attempts per 5 minutes per IP+email). Existing `/user/*` and `/admin/*` bearer routes stay as they are.

| Route | Returns / does |
|---|---|
| `GET /api/session` | `{user \| null}` |
| `GET /api/plans` | `{plans[], annualDiscount, demo}`; `demo` is `null` unless `DEMO_MODE=1` |
| `POST /api/auth/login`, `/signup` | `{redirect, user}`; one form for both roles, `redirect` is `/admin` or `/app`; signup takes `plan` |
| `POST /api/auth/logout` | revokes the token (existing `revoked_tokens`), clears the cookie |
| `GET /api/me`, `POST /api/me/plan`, `POST /api/me/rotate-key` | user + `apiKey`, `plan`, `planInfo` |
| `GET /api/state` | `{competitors[], changes[], digest, digests[], runs, lastRun, running, credits, plan, nextRun, intervalSec, mode, testSites}` |
| `POST /api/crawl` | `{skipped, changes}` (enqueue and wait, or return the running state) |
| `POST /api/competitors` `{name,url}` / `DELETE /api/competitors/{id}` | plan limit 403 message as in the UI; SSRF-validated URL |
| `GET /api/reports?period=week\|month&competitor=<id>`, `POST /api/reports/summary {period, competitor}` | report built from our events (same fields as `lib/reports.js`, including `competitorId`); `competitor` is optional and scopes changes to one competitor while crawl stats stay account-wide (UI commit ba39232). Ids must match `^\w{1,20}$`, otherwise they are ignored |
| `GET /api/admin/stats`, `GET/POST /api/admin/users`, `PATCH/DELETE /api/admin/users/{id}`, `POST .../reset-credits`, `POST .../reset-password`, `GET /api/admin/audit` | as `server.js` |
| `POST /api/test/edit`, `/api/test/reset` | demo-only (`DEMO_MODE=1`), later |
| `GET /`, `/login`, `/app`, `/admin`, `/style.css`, `/common.js` | static files from `public/`, with the same redirects |

## 3. Data shape mapping (our data → what the UI reads)
- **Competitor** `{id, name, url, site, error, snapshot, snapshots}` ← `sites` row. `snapshot` = `{ts, pagesCrawled, products, promotions[], pages[]}` from the latest `crawl_runs` plus the site's tables. **Cap `products` to the 25 most recently changed and add `productCount`**: the dashboard renders every entry in `snapshot.products`, which is fine for 3 products and unusable for 1,462. `snapshots` = number of crawl runs.
- **Change** `{id, signal, type, competitor, competitorId, ts, label, name, from, to, pct, price, text, path, sample}`. `signal` must be `product` or `page` (the reports code increments counters by it and would break otherwise).

| UI type | signal | Built from our events |
|---|---|---|
| `product_added` | product | `new_product` |
| `product_removed` | product | product missing from a full-coverage crawl |
| `price_change` | product | `price_drop` / `price_rise` (`pct` computed) |
| `new_promo` / `promo_ended` | page | `promo_started` / `promo_ended`, `on_sale` rollup |
| `new_page` / `page_removed` | page | sitemap or menu diff via the site's `pages` table |
| (stock flips) | product | extra types; the server always supplies `label`, so the UI shows them as text |

- **Digest** `{ts, changeCount, summary, actions[{title, why, priority}], source}` ← our `summarize` step (Gemini, structured output). `source` is `gemini` or `rules` (the UI's rule-based fallback is ported to Python so a Gemini outage still produces a digest). `mode` in `/api/state` is `gemini` or `rules`.
- **Run log** `crawlLog[{ts, changes, pages}]` ← `crawl_runs` (also the source for the weekly "nothing changed" entries).
- **Credits**: 1 credit per page fetched, deducted by the crawl tools, reset by plan change or admin.

## 4. Jewellery and Magento
- Our store is the project's `ours` site. First read it through public GraphQL (no token, about 15 requests for the whole catalog); switch to REST (`/rest/V1/products`, needed for cost) once the integration has `Magento_Catalog::products`.
- Competitors are entered by hand: public jewellery stores, crawled by the agent (feeds, sitemaps, JSON-LD).
- Comparison: group by category and parsed attributes (metal, carat, shape, lab-grown or natural) and compare median and range per group (ours vs theirs); exact SKU/barcode match is used when both sides share one. Our SKU stays the key for updating our own price later. Test products (for example "Test Product on Staging" at $1) are excluded by a configurable price floor.
- Price changes stay out of scope until the user asks for them. Nothing writes to Magento.

## 5. Milestone changes (see `/home/devarshi/.claude/plans/cuddly-jumping-turtle.md`)
M0 is done. New order after it: M1 crawler library → M2 repositories and change detection → M3 crawl agent → **M-UI (new, about 8h): migration 0002, cookie auth, `/api/*` routes, static serving, digest fallback, reports** → M4 queue, worker, scheduler → M5 Magento GraphQL read (REST once permitted) → M6 category/attribute comparison → M7 price change (only if requested) → M8 hardening and Docker.

## 6. Per-website scrape size (added 2026-10-09)
Each competitor website has its own limit on how many products are collected per run.

- **Stored on `sites`:** `max_products` (what the user sets) and `page_budget` (hard cap on HTTP fetches, normally left empty). NULL means "use the default" (`DEFAULT_MAX_PRODUCTS=200`, `PAGE_BUDGET=400` in settings). Migration `0002`.
- **Upper bound:** `MAX_PRODUCTS_CAP=5000`. A value above it is rejected with 400, as is anything below 1.
- **Who can set it:** the workspace owner for their own competitors, and admins for any. Set when adding (`POST /api/competitors {name, url, maxProducts?}`) and changed later with `PATCH /api/competitors/{id} {maxProducts?, pageBudget?}`. `GET /api/state` returns `maxProducts` and `pageBudget` (effective values) per competitor so the UI can show and edit them.
- **How the crawler uses it:** the limit is enforced inside the crawl tools, not by the LLM prompt. Feeds (Shopify, 250 per request) stop once `max_products` products are collected. Sitemap and listing pages stop at `max_products` or `page_budget` fetches, whichever comes first. The run record stores what was reached (`products_seen`, `limit_hit`), so the dashboard can say "collected 200 of about 1,462 (limit reached)". A raised limit takes effect on the next run and never deletes existing data; a lowered limit only stops collecting more.
- **Credits:** 1 credit per page fetched still applies, so a bigger limit costs more credits per run.
- **UI change needed (for the UI person):** add an optional "Products to scrape" number field to the Add competitor form and show `maxProducts` on the competitor card. The UI works without it (default applies).

## 7. Crawl API `/v1/*` (built 2026-10-09)
Same contract as the UI's `lib/api.js`: `x-api-key` header (per-user key shown on Account & API), envelope `{success, platform:"web", endpoint, data, credits_used, credits_remaining, request_id, cached}`, errors `{success:false, error:{type,message,status}, request_id}`.

| Route | Behaviour |
|---|---|
| `GET /v1/credits/balance` | `{balance}` |
| `GET /v1/web/scrape?url=` | `{url, status, title, markdown, links, fetched_at}`; 1 credit |
| `POST /v1/web/crawl {url, limit 1–50, max_depth 0–3, include_paths, exclude_paths, allow_external_links}` | 202 + job record; run by the worker container |
| `GET /v1/web/crawl/{job_id}` | `{job_id, kind, status queued/running/completed/failed, progress, result:{pages}, error, created_at}`; only the owner's jobs |

Differences from the Node demo, on purpose:
- **robots.txt is honoured** (Node ignored it). A disallowed start URL returns 400 `request_error`; disallowed links found during a crawl are skipped like external links.
- **Refusals are free:** the URL safety check and robots.txt run before a credit is taken. The credit is spent once the page is requested, even if the site then errors (the error message reports credits used).
- Private and internal addresses are blocked on every redirect hop, including the cloud metadata address.
- A crawl that runs out of credits fails with `insufficient credits` and keeps the pages fetched so far (same as Node).
- `markdown` is capped at 100,000 characters per page (`markdown_truncated: true` when cut).
- Users now have `plan`, `credits`, `api_key`, `name`, `last_login_at` (migration 0004). `GET /user/me` returns them.

Note: `https://louped.btdemo.biz/robots.txt` disallows all bots except Bing's, so our crawler refuses it. That's correct for a staging site. Our own store will be read through its API (Magento, step 4), not crawled.
