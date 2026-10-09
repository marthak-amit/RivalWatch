# RivalWatch: AI competitor intelligence (clickable demo)

Monitors competitor sites (or test pages you control) for two signals:

1. **Products & prices**: added/removed products, price changes with %.
2. **Pages & promotions**: new/removed pages, new/ended promotions.

A scheduled crawl stores snapshots → diffs them → an AI step writes a digest with 3–5 suggested actions.

## Run

```bash
npm start      # http://localhost:3000 (binds 127.0.0.1; set HOST=0.0.0.0 to expose). Node 20+, no dependencies
npm test
```

## The click-through

| Page | What it is |
|---|---|
| `/` | Marketing site: hero, how it works, features, **pricing** (monthly/annual toggle), FAQ |
| choose a plan | **2-second loader** → `/login?plan=…` |
| `/login` | Sign in / create account (plan preselected). Demo buttons fill credentials |
| `/app` | Customer dashboard with a **left sidebar** (Crawl now, Add competitor, Weekly report, nav, plan/credits, user). Pages: Overview, Competitors, Changes (filterable), **Reports (weekly / monthly)**, AI digests, Live demo, Account & API |
| `/admin` | Admin panel: KPIs + MRR, plan mix, search/filter users, change plan, suspend/reactivate, promote/demote, reset credits, set password, delete, add user, audit log |

Demo logins (seeded on first run): `demo@rivalwatch.dev` / `demo1234` · `admin@rivalwatch.dev` / `admin1234`
(set `ADMIN_PASSWORD` to change the admin password). Other seeded customers exist only to populate the admin table.

**Overview** has a competitor filter (chips, or a dropdown beyond 6 competitors). *All competitors* shows a side-by-side comparison table (products, price range, promos, pages, changes, last change; click a row to drill in). Selecting one competitor scopes the KPIs, chart, recent changes and AI summary to it, and shows its current pricing with the last change per plan, promotions, tracked pages and price history. The filter carries over to Reports and survives a reload.

**Reports** show changes per day (stacked: product & price vs pages & promotions), deltas vs the previous period, by-competitor breakdown, biggest price moves, highlights, an on-demand AI summary, CSV export and print/PDF. New workspaces are pre-loaded with ~30 days of clearly-labelled *sample* history so reports aren't empty in the demo; Reset in Live demo restores it.

Every user gets an isolated workspace: own crawl credits, own monitor, and own editable copies of three test competitors.
Billing is a mock: switching plans just resets the credit allowance.

## SocialCrawl-style crawl API (per-user key, shown in Account & API)

Modelled on [socialcrawl.dev](https://www.socialcrawl.dev): `x-api-key` auth, one response envelope
(`success, platform, endpoint, data, credits_used, credits_remaining, request_id, cached`), credit metering, async crawl jobs.

```bash
K='x-api-key: <your key>'
curl -H "$K" localhost:3000/v1/credits/balance
curl -H "$K" "localhost:3000/v1/web/scrape?url=https://example.com"          # page → markdown + links
curl -H "$K" -X POST localhost:3000/v1/web/crawl -H 'content-type: application/json' \
     -d '{"url":"https://example.com","max_depth":1,"limit":5,"include_paths":[],"exclude_paths":[]}'
curl -H "$K" localhost:3000/v1/web/crawl/<job_id>                            # poll status/progress/result
```

1 credit per page fetched. The crawler blocks private/internal addresses (SSRF guard), except the app's own `/test/*` pages.

## Config (env)

| Var | Meaning |
|---|---|
| `ANTHROPIC_API_KEY` | Claude writes the digest (`DIGEST_MODEL`, default `claude-sonnet-5-5`); otherwise a rule-based digest is used |
| `CRAWL_INTERVAL_SEC`, `PORT`, `HOST`, `ADMIN_PASSWORD`, `DATA_DIR` | scheduler interval (default 300), port, bind address, admin password, where `users.json` is stored |

## Layout

`server.js` routes/auth · `lib/users.js` users, scrypt passwords, sessions · `lib/plans.js` pricing · `lib/workspace.js` per-user workspace ·
`lib/crawler.js` + `lib/api.js` crawl engine and /v1 API · `lib/extract.js`, `diff.js`, `monitor.js`, `digest.js` pipeline · `lib/testsite.js` editable fake competitors · `public/` the four pages.

## Limits (demo only)

Users persist in `data/users.json`; workspaces, snapshots and sessions are in memory (restart = fresh baselines, everyone signed out).
No real payments, email, or password reset. Extraction is heuristic and static-HTML only. The Claude digest path is untested without a key.
Auth is session-cookie based with scrypt hashes, login rate limiting and JSON-only writes, but it hasn't had a security review.
