# RivalWatch backend: milestones M0–M7 and how the UI integrates

Date: 2026-10-09 · Code: `backend/` · Detailed design: `docs/superpowers/specs/` · Backend runbook: `backend/README.md`

**In one line:** the Python backend now serves the UI's own `/api/*` contract (same paths and shapes as `server.js`)
and the `public/` pages, so **the current UI runs against it unchanged**. On top of that it adds real crawling,
per-website change history, competitor settings, product search with jewellery filters, our own Magento store,
price comparison with an AI digest, and changing our own prices in Magento.

---

## Part A: What was built

### M0: Foundation
- FastAPI app, Postgres 16, Alembic migrations, everything in Docker Compose: `db`, `migrate`, `api`, `worker`, `scheduler`.
- **Accounts and roles**: `user` (self sign-up) and `admin` (created only in the database). Passwords hashed with scrypt.
- **Sessions**: every login is a row in `sessions`. Logout revokes the token. Inactivity ends a session (30 min for admins,
  8 h for users), and so do expiry (12 h / 7 days) and suspension. Admins see per-user session time.
- **Security**: login rate limit (10 failed attempts per 5 min per IP+email, cleared on success); control characters are
  rejected; SSRF guard on every outgoing request (including each redirect hop).

### M1: Crawler library and the `/v1` crawl API
- Fetching through **Scrapling**, robots.txt (RFC 9309), sitemaps (index files, `.gz`, `lastmod`, estimates for huge sites),
  product extraction (Shopify and WooCommerce feeds → JSON-LD → OpenGraph → the page's own heading and price),
  jewellery attributes from titles (carat, metal incl. "18ct gold" = 18k and plated/vermeil, shape, stone, gemstone),
  platform detection, and the page's meta tags (title, description, all `og:`/`twitter:`/`product:` tags) stored as published.
- **`/v1` crawl API** (the UI's Account & API page): `x-api-key`, 1 credit per page, async crawl jobs run by the worker.
  Refusals (private address, robots.txt) are free.

### M2: Change detection and per-website history
- **Each competitor website has its own schema `site_<id>`** with `products`, `price_history`, `crawl_runs`, `pages`,
  and **`history`**: every change ever detected, never rewritten, linked to its crawl run, its product and (for changes
  a person made) the user. Every table carries `site_id` with a foreign key and a check that it belongs to that site.
- Change types: new product, product removed, price drop/rise, on sale, out of stock / back in stock, product details
  edited (title, SKU, brand, category, image, meta title/description), new category, promotion started/ended,
  homepage changed, new page / page removed. History rows have plain columns for title, URL, path, source page,
  currency and price before/after.
- Rules that keep it honest: a product seen for the first time on a sampled site is not "new"; sitemaps that date every
  URL "today" are ignored; template values like `{{name}}` are never stored.

### M3: The crawl agent (LangGraph + Gemini)
- A **LangGraph agent** decides how to crawl each site with 6 tools (inspect site, product feed, sitemap, category listing,
  queued product pages, recheck known products). The prompts are in `agents/crawl/prompts.py`, as written in the agent spec.
- **Limits enforced in code, not by the prompt**:
  - each competitor's product limit and page budget
  - credits
  - robots.txt
  - same-site only
  - 3 refusals in a row → blocked
  - time limit
- **Pages built by JavaScript** are rendered in headless Chromium. Every request the rendered page makes passes the SSRF
  guard, so a hostile site can't reach internal addresses.
- **Gemini** words the moves ("Launched 14 products in Rings"), writes a run summary and reads pages nothing else can.
  Numbers it writes are checked against our data, and prices it reads must be printed on the page.
- **"Nothing changed" costs no AI call.** If Gemini is down, a fixed plan runs the same tools.
- Checked on real sites: missoma.com (Shopify, GBP), indriya.com (JavaScript-built, INR, rendered in the container),
  aura_jewels (no structured data, read from its HTML).

### M4: Worker and scheduler
- Postgres job queue (`FOR UPDATE SKIP LOCKED`), one worker container, retries with backoff.
- **Scheduler**: every minute it queues a crawl for each active competitor whose own cron is due (UTC, default daily
  02:00), and a catalog sync for our store. It never queues the same site twice, and skips paused/blocked sites and suspended owners.

### M-UI: the UI's `/api/*` served by the backend
- Cookie session (`rw_session`), JSON-only writes, `{"error": ...}` errors, the original redirects and CSP on the pages.
- Dashboard state, change feed, reports (port of `lib/reports.js`), rule-based digests (port of `lib/digest.js`),
  competitors with all their settings, product search, admin panel with audit log, and capability flags so the pages
  hide what isn't supported.

### M5: Our own Magento store
- Each account connects **its own Magento store** (any URL) through the UI, and the admin can set it up for a client.
- The catalog is read through **Magento's GraphQL API** (not crawled; public data needs no token, an optional token is
  stored encrypted). It syncs on the store's own schedule into its own `site_<id>` schema with the same change history,
  so our own price changes, new and removed products are logged too.
- Real store: louped.btdemo.biz, **1,462 products in ~17 s** (Engagement Rings 914, Jewelry 427, Wedding Bands 69, …).

### M6: Comparison and the AI digest
- **Comparison** (all in code): every product (ours and each competitor's) gets a jewellery type (engagement ring,
  wedding band, ring, earrings, necklace, pendant, bracelet, bangle, watch, loose diamond, set, …) from its category or
  title, plus metal family (gold, gold plated, platinum, silver…), stone (lab-grown, natural, moissanite, cubic zirconia…)
  and carat band. Groups on both sides are compared by median/min/max price with the gap in %; exact SKU/barcode matches
  are listed; types only competitors sell are listed as gaps.
- Settings per workspace: minimum price (skips test items like the $1 staging product), minimum group size, and
  **currency rates**. Without a rate, prices in another currency are shown but not compared; nothing is guessed.
- **AI digest**: after each round of crawls/syncs, the Gemini analyst writes a summary and 3-5 actions from the changes,
  the price positioning and shared SKUs. Every action must cite evidence and its numbers must appear in that evidence,
  otherwise it's dropped; topped up by the rule-based digest if fewer than 3 remain. Nothing new = no AI call.
- Real run: louped (1,462 products, USD) against missoma.com (GBP, rate 1.27): digest written in 2 s, all actions with
  real evidence.

### M7: Changing our prices in Magento
- **Request, then apply.** A price change is first *requested*: nothing changes in Magento yet. The answer is a
  **preview**: the live price and cost read from Magento, change %, margin before/after, and where the new price puts us
  against competitors (the medians for that jewellery type, and the same product where a competitor sells it). It is made
  real by a separate **apply**.
- **Magento first.** Applying re-reads the live price and cost, re-checks the guardrails and writes the price through
  Magento's price API (`POST /rest/V1/products/base-prices`: only the price changes, nothing else on the product). Only when
  Magento confirms is our copy updated and the change logged in our store's history under the person who applied it. If
  Magento refuses, nothing changes anywhere: the change is `failed` with Magento's own error.
- **Guardrails** per workspace: one change may move a price at most `maxChangePct` (default 50%, catches typos like an
  extra zero); the margin over cost must stay at least `minMarginPct` (default 0 = never below cost; checked when Magento
  has a cost); with `adminOnly`, only the agency (an admin) applies a client's changes, and the client's requests wait for
  them.
- **Revert**: one call puts back the price a change replaced, as a change of its own.
- **Audit**: every change keeps who requested it, who applied it, old and new price, status and Magento's error.
- What changes is the **regular** (base) price at the default scope. A sale price stays in place (the preview warns).
- **Needs**, in the Magento admin: (1) System → Integrations → the integration → API: **Catalog → Inventory → Products**;
  (2) Stores → Configuration → Services → OAuth → Consumer Settings: **Allow OAuth Access Tokens to be used as standalone
  Bearer tokens = Yes** (Magento 2.4.4+). Checked on louped (read-only call): today its token gets
  `401 The consumer isn't authorized to access Magento_Catalog::catalog`, so requests work with the last synced price
  and applying returns that error until both are set.
- The token is now used **only** for the price APIs. The catalog sync never sends it: louped's GraphQL fails any query that
  carries an integration token ("Composite reader could not read a token"), and the catalog is public anyway.

**Tests**: 173 (unit, integration against Postgres, crawl agent with fake models, UI API end to end, a stand-in
Magento for the price APIs). The UI team's own
suite (`integration/backend.integration.mjs`) passes 53 of 54 checks; the one difference is intended (see Part C).

---

## Part B: Integrating the UI

### 1. Run it

```bash
cd backend
cp .env.example .env                        # set JWT_SECRET (32+ chars), APP_ENCRYPTION_KEY, GEMINI_API_KEY
docker --context default compose up -d --build
```

| What | URL |
|---|---|
| UI pages (served by the backend) | http://localhost:8031/ · `/login` · `/app` · `/admin` |
| Swagger, every route with try-it-out | http://localhost:8031/docs |
| Health | http://localhost:8031/health |

Admins are created only in the database:
`docker --context default compose exec api python scripts/create_admin.py boss@example.com 'long-password' | docker --context default compose exec -T db psql -U postgres -d competitor`

### 2. Two ways to connect

| | Direct (recommended) | Node BFF (`BACKEND_URL`) |
|---|---|---|
| How | Open the backend's URL; it serves `public/` and `/api/*` | `server.js` proxies `/user`, `/admin`, `/v1` |
| Competitors, history, reports | From Postgres (survive restarts, shared) | Node's in-memory pipeline |
| New features (settings, products search, our store) | Available | Not wired in the BFF |

### 3. Rules every `/api/*` call follows

- **Session**: `POST /api/auth/login` or `/signup` sets the `rw_session` cookie (HttpOnly, SameSite=Lax, Secure behind
  HTTPS). Send requests with `credentials: 'same-origin'` (common.js already does).
- **Non-GET requests must be JSON** (`content-type: application/json`, even with an empty `{}` body, including DELETE),
  otherwise 415. Same CSRF defence as the original server.
- **Errors** are `{"error": "message"}`: 400 validation, 401 not signed in, 403 forbidden or plan limit, 404 not found,
  409/400 duplicate, 415 not JSON, 429 too many attempts.
- **Sessions end** after logout, expiry, inactivity or suspension; any `/api` call then answers 401.
- Competitor ids are **strings** (`"752"`), user ids are numbers.

### 4. Capability flags (`GET /api/plans` → `backend`)

```json
{"enabled": true, "demo": false, "signupName": true, "planChanges": true, "rotateKey": true,
 "adminPlanEdit": true, "adminRoleEdit": false, "adminResetCredits": true, "adminResetPassword": true}
```
`demo` is false: every competitor is a real website, so the Live demo tab hides itself. `adminRoleEdit` is false:
admins are managed in the database. The login-page `demo` object is always `{"user": null, "admin": null}`.

### 5. Endpoints

#### Auth and account
| Call | Body | Answer |
|---|---|---|
| `POST /api/auth/signup` | `{email, password, name?, plan?}` | `{redirect: "/app", user}`; name and plan are stored; credits = the plan's allowance |
| `POST /api/auth/login` | `{email, password}` | `{redirect: "/app" or "/admin", user}` · 401 `Incorrect email or password` · 403 `This account is suspended. Contact support.` · 429 after 10 failures in 5 min |
| `POST /api/auth/logout` | `{}` | `{ok: true}`, the session is revoked server-side |
| `GET /api/session` | | `{user: {...} or null}` |
| `GET /api/me` | | user + `apiKey`, `plan`, `planInfo`, `credits` |
| `POST /api/me/plan` | `{plan}` | me; credits reset to the new plan's allowance (mock checkout) |
| `POST /api/me/rotate-key` | `{}` | me with a new `apiKey` |

`user` = `{id, email, name, role, plan, status: "active"|"suspended", createdAt, lastLoginAt}`.

#### Dashboard
**`GET /api/state`** →
```
{competitors: [Competitor], changes: [Change] (newest first, up to 1000), digest, digests (one per day with changes),
 runs, lastRun, running, credits, plan, nextRun (ms epoch), intervalSec, mode: "gemini"|"rules", testSites: [],
 ★store: Store | null}
```

**Competitor** (everything the UI already reads, plus new fields marked ★):
```
{id, name, url, site: null, enabled, error, since, snapshots,
 snapshot: null | {ts, pagesCrawled, products: {<url>: {name, price: "£80.00", amount: 80}}  (25 most recently seen),
                   promotions: [..], pages: [paths], profile: {headline, description, positioning, socials, emails, keyPages},
                   ★productCount, ★estTotal (catalog size estimate), ★summary (last run in one line), ★inProgress},
 ★status: "active"|"paused"|"blocked", ★crawling, ★progress: {pages, products} while a crawl runs, ★lastRun, ★platform,
 ★maxProducts, ★pageBudget, ★categories: [{name, url}], ★sort, ★cron, ★nextRun, ★crawlSettings: {...}}
```
While the first crawl runs, `snapshot` already shows what has been found (`inProgress: true`).

**Change**: `{id, competitor, competitorId, ts, signal: "product"|"page", type, label, sample: false, ★kind, ★category, ★url}`
plus fields per type. `label` is always a ready-to-show sentence.

| `type` | signal | extra fields | example label |
|---|---|---|---|
| `product_added` | product | name, price | New product: Oval Halo Ring at £750.00 |
| `product_removed` | product | name, price | Product removed: Pearl Pendant (was £450.00) |
| `price_change` | product | name, from, to, pct | Stud Earrings: £300.00 → £240.00 (-20%) |
| ★`on_sale` | product | name, price, was, pct | On sale: Pave Band £80.00 (was £100.00) (-20%) |
| ★`out_of_stock` / ★`back_in_stock` | product | name | Out of stock: Halo Ring |
| ★`product_updated` | product | name, fields | Updated meta_title: Pave Band |
| `new_promo` / `promo_ended` | page | text | New promotion: "20% off earrings" |
| `new_page` / `page_removed` | page | path | New page: /pages/black-friday |
| ★`new_category` | page | name, path | New category: Lab-Grown |
| ★`page_changed` | page | | Homepage changed |

| Call | Body | Answer |
|---|---|---|
| `POST /api/crawl` | `{}` | queues every enabled competitor, waits up to 90 s: `{skipped, queued, pending, changes}` |
| `GET /api/reports?period=week\|month&competitor=<id>` | | same shape as `lib/reports.js` (`daily`, `byType`, `byCompetitor`, `biggestMoves`, `highlights`, `crawls`, ...) |
| `POST /api/reports/summary` | `{period, competitor?}` | `{summary, actions: [{title, why, priority}], source: "rules"}` |

#### Competitors
| Call | Body | Answer |
|---|---|---|
| `POST /api/competitors` | `{name?, url, maxProducts?, pageBudget?, categories?, sort?, cron?, enabled?, crawlSettings?}` | `{ok, id, competitor}`; first crawl starts at once · 400 bad/private URL or duplicate · 403 plan limit |
| `PATCH /api/competitors/{id}` | any of the same fields | `{ok, enabled, competitor}`; `enabled: true` crawls now |
| `DELETE /api/competitors/{id}` | `{}` | `{ok: true}`; its data is deleted |
| ★`GET /api/competitors/{id}/categories` | | `{categories: [{name, url}]}`: the site's own menu (after its first crawl), for the scope picker |

Settings (all optional; empty = server default):
- `maxProducts` (1-5000, default 200), `pageBudget` (1-5000, default 400)
- `categories`: `[{name, url}]` taken from `/categories` above; `[]` = whole site
- `sort`: `relevance` (site order) · `newest` · `price_asc` · `price_desc` · `discount`: decides which products fill the limit
- `cron`: e.g. `"0 2 * * *"` (UTC, default daily 02:00)
- `crawlSettings`: `{delay_sec 0.2-10, time_limit_sec 60-3600, browser: "auto"|"off", browser_pages 0-200,
  product_meta: bool, ai_extract: bool, ai_extract_cap 0-100}`; on PATCH only the keys sent change, `null` resets a key

#### ★ Our store (Magento)
| Call | Body | Answer |
|---|---|---|
| `GET /api/store` | | `{store: null}` or `{store: Store}` |
| `PUT /api/store` | `{url, storeCode?, token?, cron?}` | `{store}`; checks the store answers, then the first sync starts · 400 with the store's own error (e.g. not a Magento store, unauthorized) |
| `PATCH /api/store` | `{storeCode?, token?, cron?, enabled?}` | `{store}`; change settings without reconnecting. `token: ""`/`null` removes it, leaving it out keeps it; a new store view is tested first (400 keeps the old one); the token is checked when it's first used (price changes); `enabled: false` pauses the sync |
| `POST /api/store/sync` | `{}` | `{queued, store}` · 404 when no store is connected |
| `DELETE /api/store` | `{}` | `{ok: true}`; its synced data is deleted |

`Store` = `{id, platform: "magento", url, name, storeCode, currency, hasToken, enabled, status: "connected"|"error", error,
syncing, productCount, lastSync, lastSummary, cron, nextSync}`. Any Magento store works; each account connects its own.
The token is optional: only price changes (M7) use it, the catalog is read without it. Stored encrypted, never returned.
Our store's products: `GET /api/products?competitor=ours` (same filters and facets as below).

#### ★ Product search (the jewellery filters)
`GET /api/products?competitor=&category=&metal=&gem=&stone=&min_price=&max_price=&on_sale=&in_stock=&q=&sort=&limit=&offset=`

- `competitor`: a competitor id, `ours` for our store, or empty for all competitors
- `metal` matches inside the name ("gold" finds "18k gold plated"); `q` matches word starts ("ring" finds Rings, not Earrings)
- `sort`: `relevance`, `newest`, `price_asc`, `price_desc`, `discount`; `limit` 1-200, `offset` up to 10000

```
{products: [{competitorId, competitor, title, url, path, price, priceLabel: "£80.00", wasPrice, discountPct, currency,
             inStock, category, attributes: {metal, gem, stone, carat, shape}, image, metaTitle, firstSeen}],
 total, limit, offset, facets: {categories, metals, gems, stones, currencies}}   <- facets fill the filter dropdowns
```

#### ★ Comparison (M6)
| Call | Body | Answer |
|---|---|---|
| `GET /api/comparison?group_by=&category=&competitor=` | | see below; `group_by`: `type` (default) · `type,metal` · `type,stone` · `type,metal,stone` · `type,carat` |
| `GET /api/comparison/settings` | | `{min_price, min_group_size, fx_rates}` |
| `PATCH /api/comparison/settings` | `{minPrice?, minGroupSize?, fxRates?: {"GBP": 1.27}}` | the settings; rates convert into our store's currency |

```
{ours: {siteId, name, currency, products} | null, settings, groupBy,
 rows: [{key: {type, metal?, stone?, carat?}, label: "engagement ring · lab-grown diamond",
         ours: {currency, count, median, min, max},
         competitors: [{competitorId, name, currency, count, median, min, max, comparable, medianInOurCurrency?, gapPct?}],
         cheapest: "ours" | <competitorId>}],                       <- sorted by the biggest gap
 onlyCompetitors: [{key, label, competitors: [...]}],                  <- what they sell and we don't
 matches: [{sku, title, ours: {price, currency, url}, competitorId, competitor, theirs: {...}, gapPct}],
 notes: ["LondonCo prices are in GBP; add an fx_rates entry for GBP ..."]}
```
`gapPct` < 0 means the competitor is cheaper. With no store connected, `ours` is null and `notes` says how to start.

**Digests** (`state.digest`, `state.digests`): now stored; `source` is `gemini`, `gemini+rules` or `rules`, and each
action has `evidence` (ids such as `e:752-15` for a change, `p:3` for a positioning row, `s:<sku>` for a shared SKU).
`POST /api/reports/summary` uses the analyst when Gemini is configured.

#### ★ Our prices in Magento (M7)
| Call | Body | Answer |
|---|---|---|
| `GET /api/prices?status=` | | `{changes: [Change], settings}`; `status`: `pending` · `applied` · `failed` · `cancelled` |
| `POST /api/prices` | `{sku, newPrice}` | `{change, preview}`; a pending change (replaces an earlier pending one for that SKU) · 400 guardrail (message says which) · 404 SKU not in our catalog / no store |
| `POST /api/prices/{id}/apply` | `{}` | `{change}` applied · 502 Magento refused (change is `failed`, nothing changed) · 403 only the agency applies · 409 not pending |
| `POST /api/prices/{id}/cancel` | `{}` | `{change}` · 404 when it isn't pending |
| `POST /api/prices/{id}/revert` | `{}` | `{change, preview}`: puts the old price back (applied at once, or pending when only the agency applies) |
| `GET /api/prices/settings` | | `{maxChangePct, minMarginPct, adminOnly}` |
| `PATCH /api/prices/settings` | `{maxChangePct?, minMarginPct?}` | the settings · 403 when the agency applies changes (`adminOnly`) |

```
Change  = {id, sku, title, url, currency, oldPrice, newPrice, changePct, status, error, requestedBy, appliedBy, client,
           reverts, createdAt, appliedAt}
preview = {currentPrice, newPrice, changePct, priceSource: "magento" | "catalog", salePrice, shownBefore, shownAfter,
           cost, marginBefore, marginAfter, canApply, settings, warnings: [...],
           position: {type: "engagement ring", competitors: [{name, median, gapBefore, gapAfter}],
                      sameProduct: [{name, price, url, gapBefore, gapAfter}], notes}}
```
`priceSource: "catalog"` means Magento's live price couldn't be read (the reason is in `warnings`): the preview uses the
last synced price, and applying will fail until the token has the permission. `shownAfter` is what shoppers pay (a sale
price stays). `gapBefore`/`gapAfter` < 0: the competitor is cheaper. Products now include `sku`, so a "Change price"
button on our store's products page has what it needs.

#### Admin (`/api/admin/*`, admin session only)
| Call | Body | Answer |
|---|---|---|
| `GET /api/admin/stats` | | `{total, active, suspended, admins, byPlan, mrr, newThisWeek}` |
| `GET /api/admin/users?q=&plan=&status=` | | `{users: [user + credits, creditLimit, competitors]}` |
| `POST /api/admin/users` | `{email, name?, password, plan?}` | the new user · `role: "admin"` → 403 |
| `PATCH /api/admin/users/{id}` | `{plan?, status?: "active"\|"suspended"}` | the user; suspending ends their sessions · admins → 403 |
| `DELETE /api/admin/users/{id}` | `{}` | `{ok: true}`; their workspace and data go too |
| `POST /api/admin/users/{id}/reset-credits` | `{}` | the user |
| `POST /api/admin/users/{id}/reset-password` | `{password}` | `{ok: true}`; their sessions end |
| `GET /api/admin/audit` | | `{audit: [{ts, actor, action, target, detail}]}` (stored, survives restarts) |
| `GET /api/admin/users/{id}/store` | | `{store}` of that client (null if none) |
| `PUT /api/admin/users/{id}/store` | `{url, storeCode?, token?, cron?}` | connect a client's store for them (audited) |
| `PATCH /api/admin/users/{id}/store` | `{storeCode?, token?, cron?, enabled?}` | change a client's store settings (audited) |
| `POST /api/admin/users/{id}/store/sync` · `DELETE /api/admin/users/{id}/store` | `{}` | sync now / disconnect (audited) |
| `GET /api/admin/prices?status=&user_id=` | | `{changes}` of every client (`status=pending`: what waits for the agency; `client` is the owner's email) |
| `POST /api/admin/users/{id}/prices` | `{sku, newPrice}` | `{change, preview}`: request a change for a client (audited) |
| `POST /api/admin/prices/{changeId}/apply` · `/cancel` · `/revert` | `{}` | as the client calls, for any client (audited) |
| `GET` · `PATCH /api/admin/users/{id}/prices/settings` | `{maxChangePct?, minMarginPct?, adminOnly?}` | a client's guardrails; `adminOnly: true` makes the agency the one who applies (audited) |

#### Crawl API for API-key users (unchanged contract)
`/v1/credits/balance`, `/v1/web/scrape`, `/v1/web/crawl`, `/v1/web/crawl/{job_id}`. Scrape results now include
`description` (the page's meta description).

### 6. Suggested UI work (the current pages run without it)

1. **Competitor settings form** on the competitor card: product limit, scope (`GET .../categories` as a checklist), sort,
   schedule, and an "Advanced" block for `crawlSettings`.
2. **Products page** with the filters from `/api/products` (category, metal, gemstone, stone, price range, on sale, in
   stock, search, sort) and its `facets` for the dropdowns, with a switch between competitors and our store.
3. **Our store panel**: connect form (`PUT /api/store`), status, product count, last sync, schedule, pause, "Sync now";
   and the same block on the admin's user page (`/api/admin/users/{id}/store`).
4. Show crawl progress on the card (`crawling`, `progress`, `snapshot.inProgress`) and "collected X of ~Y"
   (`productCount` / `estTotal`).
5. **Comparison page**: table of `rows` (our median vs each competitor, gap colored), a "They sell, we don't" list
   (`onlyCompetitors`), shared-SKU matches, a group-by switch, and a settings form (minimum price, currency rates).
6. Show the new change types (`on_sale`, stock, `new_category`, `product_updated`, `page_changed`); their `label` is
   ready to display.
7. **Change price** (our store's products and the comparison's shared SKUs): a form for the new price, then the preview
   (margin, change %, position against competitors, warnings) with "Apply" / "Cancel"; a **price changes** list with
   status, who requested/applied, errors and "Revert"; a guardrails form. On the admin side: a "waiting for approval" list
   (`GET /api/admin/prices?status=pending`) and the `adminOnly` switch on the client's page.

---

## Part C: Changes for the UI team's code

- `app.html`: `go()` → `paintView()` can run before the first `/api/state` arrives (e.g. opening `/app#competitors`
  right after sign-in), throwing "Cannot read properties of null (reading 'competitors')". A guard such as
  `if (!S) return;` at the top of `paintView()` fixes it.
- `integration/backend.integration.mjs` line 88 expects 401 for a suspended user's login; the backend now answers
  403 "Account disabled" (asked for in `backend-integration.md` item 6), so the check should accept 403.
- `backend-integration.md` items now done in the backend: 1 (`/api/*`), 2 (signup stores name/plan, `/user/me` has
  `created_at`), 3 (plan change, key rotation), 4 (admin list with name/plan/credits, plan edits, reset credits/password,
  audit log), 5 (scrape `description`), 6 (403 for disabled accounts), 7 (control characters → 4xx, not 500),
  8 (backend login rate limit: 10 failures per 5 min per IP+email, cleared on success), 9 (scheduled crawls skip
  suspended owners).

## Part D: Not built yet

| Milestone | What |
|---|---|
| M8 | Production readiness: no public database port, secrets, rate limits stored in the database, backups, CI, deployment |
