// SocialCrawl-style public API over the built-in crawler:
//   x-api-key auth, one response envelope, credit accounting.
//   GET  /v1/web/scrape?url=         POST /v1/web/crawl {url, limit, max_depth, ...}
//   GET  /v1/web/crawl/:job_id       GET  /v1/credits/balance
import { randomUUID } from 'node:crypto';

export function makeApi(crawler, apiKey) {
  const envelope = (endpoint, data, before) => ({
    success: true, platform: 'web', endpoint, data,
    credits_used: before - crawler.credits, credits_remaining: crawler.credits,
    request_id: 'req_' + randomUUID().slice(0, 12), cached: false,
  });
  const fail = (status, type, message) => ({
    status, body: { success: false, error: { type, message, status }, request_id: 'req_' + randomUUID().slice(0, 12) },
  });

  /** @returns {Promise<{status:number, body:object}>} */
  return async function handle(method, url, headers, body) {
    if (headers['x-api-key'] !== apiKey) return fail(401, 'auth_error', 'Missing or invalid x-api-key header');
    const before = crawler.credits;
    const p = url.pathname;
    try {
      if (method === 'GET' && p === '/v1/credits/balance') return { status: 200, body: envelope(p, { balance: crawler.credits }, before) };
      if (method === 'GET' && p === '/v1/web/scrape') {
        const target = url.searchParams.get('url');
        if (!target) return fail(400, 'validation_error', '`url` is required');
        return { status: 200, body: envelope(p, await crawler.scrape(target), before) };
      }
      if (method === 'POST' && p === '/v1/web/crawl') {
        if (!body?.url) return fail(400, 'validation_error', '`url` is required');
        return { status: 202, body: envelope(p, crawler.startCrawl(body.url, body), before) };
      }
      const m = p.match(/^\/v1\/web\/crawl\/([\w-]+)$/);
      if (method === 'GET' && m) {
        const job = crawler.jobs.get(m[1]);
        return job ? { status: 200, body: envelope(p, job, before) } : fail(404, 'not_found', 'Unknown job_id');
      }
      return fail(404, 'not_found', 'Unknown endpoint');
    } catch (e) {
      return fail(e.status || 400, e.status === 402 ? 'insufficient_credits' : 'request_error', e.message);
    }
  };
}
