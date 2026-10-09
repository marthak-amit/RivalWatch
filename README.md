# RivalWatch: AI competitor intelligence agent (demo)

Monitors 3 competitor sites (or test pages you control) for two signals:

1. **Products & prices**: added/removed products, price changes (with %).
2. **Pages & promotions**: new/removed pages, new/ended promotions.

A scheduled crawl stores snapshots → diffs them → an AI step writes a digest with 3–5 suggested actions.
The demo UI edits a test competitor page live so you can watch detection happen.

## Run

```bash
npm start            # http://localhost:3000, no dependencies, Node 20+
npm test
```

1. Open the page. The first crawl captures a baseline.
2. Pick a competitor, click e.g. **Cut a price 20%** / **Launch promotion** / **Publish new page**.
3. Click **Crawl now** (it also auto-crawls every `CRAWL_INTERVAL_SEC`, default 300).
4. See the detected changes and the digest with suggested actions.

## SocialCrawl-style crawl service (built in)

Modelled on [socialcrawl.dev](https://www.socialcrawl.dev): `x-api-key` auth, one response envelope
(`success, platform, endpoint, data, credits_used, credits_remaining, request_id, cached`), credit metering,
and an async crawl job. RivalWatch's own scheduler runs on top of it.

```bash
K='x-api-key: rw_demo_key'     # override with RIVALWATCH_API_KEY
curl -H "$K" localhost:3000/v1/credits/balance
curl -H "$K" "localhost:3000/v1/web/scrape?url=http://localhost:3000/test/acme/pricing"   # page → markdown + links
curl -H "$K" -X POST localhost:3000/v1/web/crawl -H 'content-type: application/json' \
     -d '{"url":"http://localhost:3000/test/acme/","max_depth":1,"limit":5,"include_paths":["/test"],"exclude_paths":[]}'
curl -H "$K" localhost:3000/v1/web/crawl/<job_id>                                          # poll status/progress/result
```

Costs: 1 credit per page fetched; 500 demo credits. The crawler blocks private/internal addresses (SSRF guard),
except this app's own `/test/*` pages.

## Config (env)

| Var | Meaning |
|---|---|
| `COMPETITORS` | `Name=https://url;Name2=https://url2` to monitor real sites instead of the test pages |
| `ANTHROPIC_API_KEY` | Use Claude for the digest (`DIGEST_MODEL`, default `claude-sonnet-5-5`); otherwise a rule-based digest is used |
| `CRAWL_INTERVAL_SEC`, `PORT`, `RIVALWATCH_API_KEY`, `PERSIST=1` | scheduler interval, port, API key, reload `data/state.json` on start |

## Layout

`lib/crawler.js` crawl engine · `lib/api.js` /v1 API · `lib/extract.js` markdown → products/promos/pages ·
`lib/diff.js` snapshot diff · `lib/monitor.js` scheduled pipeline · `lib/digest.js` AI step · `lib/testsite.js` editable fake competitors.

## Limits (demo)

Extraction is heuristic (price regex + promo keywords on markdown), static HTML only (no JS rendering),
state is in memory unless `PERSIST=1`, and the Claude digest path is untested without a key.
