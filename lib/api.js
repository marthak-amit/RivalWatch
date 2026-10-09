// SocialCrawl-style public API over the built-in crawler:
//   x-api-key auth (per-user key), one response envelope, credit accounting.
//   GET  /v1/web/scrape?url=         POST /v1/web/crawl {url, limit, max_depth, ...}
//   GET  /v1/web/crawl/:job_id       GET  /v1/credits/balance
import { randomUUID } from 'node:crypto';

const rid = () => 'req_' + randomUUID().slice(0, 12);

/** @param {(key:string)=>import('./crawler.js').Crawler|null} lookup maps an API key to that user's crawler */
export function makeApi(lookup) {
  const fail = (status, type, message) => ({ status, body: { success: false, error: { type, message, status }, request_id: rid() } });

  return async function handle(method, url, headers, body) {
    const crawler = lookup(headers['x-api-key']);
    if (!crawler) return fail(401, 'auth_error', 'Missing or invalid x-api-key header');
    const before = crawler.credits;
    const p = url.pathname;
    const ok = (status, endpoint, data) => ({ status, body: {
      success: true, platform: 'web', endpoint, data,
      credits_used: before - crawler.credits, credits_remaining: crawler.credits, request_id: rid(), cached: false } });
    try {
      if (method === 'GET' && p === '/v1/credits/balance') return ok(200, p, { balance: crawler.credits });
      if (method === 'GET' && p === '/v1/web/scrape') {
        const target = url.searchParams.get('url');
        if (!target) return fail(400, 'validation_error', '`url` is required');
        return ok(200, p, await crawler.scrape(target));
      }
      if (method === 'POST' && p === '/v1/web/crawl') {
        if (!body?.url) return fail(400, 'validation_error', '`url` is required');
        return ok(202, p, crawler.startCrawl(body.url, body));
      }
      const m = p.match(/^\/v1\/web\/crawl\/([\w-]+)$/);
      if (method === 'GET' && m) {
        const job = crawler.jobs.get(m[1]);
        return job ? ok(200, p, job) : fail(404, 'not_found', 'Unknown job_id');
      }
      return fail(404, 'not_found', 'Unknown endpoint');
    } catch (e) {
      return fail(e.status || 400, e.status === 402 ? 'insufficient_credits' : 'request_error', e.message);
    }
  };
}
