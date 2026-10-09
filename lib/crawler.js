// Built-in crawl engine, modelled on SocialCrawl's web endpoints:
//   scrape(url)            ~ GET  /v1/web/scrape
//   startCrawl(url, opts)  ~ POST /v1/web/crawl   (async job, polled by id)
// Options mirror theirs: limit, max_depth, include_paths, exclude_paths,
// allow_external_links.
import dns from 'node:dns/promises';
import net from 'node:net';
import { randomUUID } from 'node:crypto';
import { htmlToMarkdown } from './markdown.js';

const UA = 'RivalWatchBot/0.1 (+competitor monitoring demo)';
const isPrivateIp = (ip) => {
  if (net.isIPv6(ip)) return ip === '::1' || /^f[cd]/i.test(ip) || /^fe80/i.test(ip) || ip.startsWith('::ffff:');
  const [a, b] = ip.split('.').map(Number);
  return a === 10 || a === 127 || a === 0 || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168) || (a === 169 && b === 254);
};

export class Crawler {
  /** @param {{allowLocal?: (u:URL)=>boolean, credits?: number}} opts */
  constructor({ allowLocal = () => false, credits = 500 } = {}) {
    this.allowLocal = allowLocal; // lets the app's own /test pages through the SSRF guard
    this.credits = credits;
    this.jobs = new Map();
  }

  async #guard(u) {
    if (!/^https?:$/.test(u.protocol)) throw new Error('only http(s) URLs are allowed');
    if (this.allowLocal(u)) return;
    const addrs = net.isIP(u.hostname) ? [{ address: u.hostname }] : await dns.lookup(u.hostname, { all: true });
    if (addrs.some((a) => isPrivateIp(a.address))) throw new Error('private/internal addresses are blocked');
  }

  async #fetchHtml(url) {
    let u = new URL(url);
    for (let hop = 0; hop < 4; hop++) {
      await this.#guard(u);
      const res = await fetch(u, { redirect: 'manual', headers: { 'user-agent': UA, accept: 'text/html' }, signal: AbortSignal.timeout(15_000) });
      if (res.status >= 300 && res.status < 400 && res.headers.get('location')) {
        u = new URL(res.headers.get('location'), u);
        continue;
      }
      const type = res.headers.get('content-type') || '';
      if (!/html|text/.test(type)) throw new Error(`unsupported content-type ${type}`);
      return { status: res.status, html: (await res.text()).slice(0, 2_000_000), finalUrl: u.href };
    }
    throw new Error('too many redirects');
  }

  #charge(n) {
    if (this.credits < n) throw Object.assign(new Error('insufficient credits'), { status: 402 });
    this.credits -= n;
  }

  /** Scrape one page. Costs 1 credit. */
  async scrape(url) {
    this.#charge(1);
    const { status, html, finalUrl } = await this.#fetchHtml(url);
    const { title, description, markdown, links } = htmlToMarkdown(html, finalUrl);
    return { url: finalUrl, status, title, description, markdown, links, fetched_at: new Date().toISOString() };
  }

  /** Start an async crawl job; returns the job record immediately. */
  startCrawl(url, { limit = 10, max_depth = 1, include_paths = [], exclude_paths = [], allow_external_links = false } = {}) {
    new URL(url); // validate early
    limit = Math.min(Math.max(+limit || 10, 1), 50);
    const job = {
      job_id: 'crawl_' + randomUUID().slice(0, 12), kind: 'crawl', status: 'queued',
      progress: { completed: 0, total: limit }, result: null, error: null, created_at: new Date().toISOString(),
    };
    this.jobs.set(job.job_id, job);
    this.#runCrawl(job, url, { limit, max_depth: Math.min(+max_depth || 0, 3), include_paths, exclude_paths, allow_external_links });
    return job;
  }

  async #runCrawl(job, root, o) {
    job.status = 'running';
    const rootUrl = new URL(root);
    const pathOk = (u) => {
      if (o.include_paths.length && !o.include_paths.some((p) => u.pathname.startsWith(p))) return false;
      return !o.exclude_paths.some((p) => u.pathname.startsWith(p));
    };
    const seen = new Set([rootUrl.href]);
    let queue = [{ url: rootUrl.href, depth: 0 }];
    const pages = [];
    try {
      while (queue.length && pages.length < o.limit) {
        const { url, depth } = queue.shift();
        let page;
        try { page = await this.scrape(url); } catch (e) {
          if (e.status === 402) throw e;
          pages.push({ url, error: e.message, depth });
          continue;
        }
        pages.push({ ...page, depth });
        job.progress.completed = pages.length;
        if (depth < o.max_depth) {
          for (const l of page.links) {
            const lu = new URL(l); lu.hash = '';
            if (seen.has(lu.href)) continue;
            if (!o.allow_external_links && lu.origin !== rootUrl.origin) continue;
            if (lu.origin === rootUrl.origin && !pathOk(lu)) continue;
            seen.add(lu.href);
            queue.push({ url: lu.href, depth: depth + 1 });
          }
        }
      }
      job.result = { pages };
      job.status = 'completed';
    } catch (e) {
      job.error = e.message; job.status = 'failed'; job.result = { pages };
    }
    job.progress.total = pages.length;
  }

  async waitForJob(id, timeoutMs = 60_000) {
    const t0 = Date.now();
    for (;;) {
      const j = this.jobs.get(id);
      if (j.status === 'completed' || j.status === 'failed') return j;
      if (Date.now() - t0 > timeoutMs) throw new Error('crawl timed out');
      await new Promise((r) => setTimeout(r, 50));
    }
  }
}
