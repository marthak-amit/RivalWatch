import http from 'node:http';
import fs from 'node:fs';
import { Crawler } from './lib/crawler.js';
import { makeApi } from './lib/api.js';
import { Monitor } from './lib/monitor.js';
import * as testsite from './lib/testsite.js';

const PORT = +process.env.PORT || 3000;
const API_KEY = process.env.RIVALWATCH_API_KEY || 'rw_demo_key';
const INTERVAL = (+process.env.CRAWL_INTERVAL_SEC || 300) * 1000;

// Our own /test/* pages bypass the crawler's SSRF guard; everything else is checked.
const crawler = new Crawler({
  allowLocal: (u) => ['localhost', '127.0.0.1'].includes(u.hostname) && +u.port === PORT && u.pathname.startsWith('/test/'),
});
const api = makeApi(crawler, API_KEY);

// COMPETITORS="Name=https://url;Name2=https://url2" monitors real sites; default is the 3 test sites.
const competitors = process.env.COMPETITORS
  ? process.env.COMPETITORS.split(';').map((s, i) => { const [name, url] = s.split(/=(.+)/); return { id: 'c' + i, name: name.trim(), url: url.trim() }; })
  : Object.entries(testsite.sites).map(([id, s]) => ({ id, name: s.name, url: `http://localhost:${PORT}/test/${id}/` }));
const monitor = new Monitor(crawler, competitors);

let nextRun = Date.now() + INTERVAL;
const tick = async () => { nextRun = Date.now() + INTERVAL; await monitor.run().catch((e) => console.error('crawl failed', e)); };
setInterval(tick, INTERVAL);

const readJson = (req) => new Promise((resolve) => {
  let b = ''; req.on('data', (d) => { b += d; if (b.length > 1e5) req.destroy(); });
  req.on('end', () => { try { resolve(JSON.parse(b || '{}')); } catch { resolve({}); } });
});
const send = (res, status, body, type = 'application/json') => {
  res.writeHead(status, { 'content-type': type });
  res.end(type === 'application/json' ? JSON.stringify(body) : body);
};

http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://localhost:${PORT}`);
  const p = url.pathname;
  try {
    if (p === '/') return send(res, 200, fs.readFileSync('public/index.html', 'utf8'), 'text/html; charset=utf-8');

    if (p.startsWith('/v1/')) {
      const out = await api(req.method, url, req.headers, req.method === 'POST' ? await readJson(req) : null);
      return send(res, out.status, out.body);
    }

    const t = p.match(/^\/test\/(\w+)\/?(.*)$/);
    if (t && req.method === 'GET') {
      const html = testsite.render(t[1], t[2].replace(/\/$/, ''));
      return html ? send(res, 200, html, 'text/html; charset=utf-8') : send(res, 404, 'not found', 'text/plain');
    }

    if (p === '/api/state') {
      return send(res, 200, { ...monitor.view(), credits: crawler.credits, intervalSec: INTERVAL / 1000, nextRun, mode: process.env.ANTHROPIC_API_KEY ? 'claude' : 'rules',
        testSites: process.env.COMPETITORS ? null : Object.entries(testsite.sites).map(([id, s]) => ({ id, name: s.name })) });
    }
    if (p === '/api/crawl' && req.method === 'POST') {
      const r = await monitor.run();
      return send(res, 200, { skipped: r.skipped, changes: r.changes.length });
    }
    if (p === '/api/test/edit' && req.method === 'POST') {
      if (process.env.COMPETITORS) return send(res, 400, { error: 'test edits only apply to the built-in test sites' });
      const { site, action } = await readJson(req);
      return send(res, 200, { edited: testsite.edit(site, action) });
    }
    if (p === '/api/test/reset' && req.method === 'POST') {
      testsite.reset(); monitor.reset(); crawler.credits = 500;
      await monitor.run(); // new baseline
      return send(res, 200, { ok: true });
    }
    send(res, 404, { error: 'not found' });
  } catch (e) {
    send(res, 400, { error: e.message });
  }
}).listen(PORT, () => {
  console.log(`RivalWatch on http://localhost:${PORT}  (digest: ${process.env.ANTHROPIC_API_KEY ? 'Claude' : 'rule-based'}, crawl every ${INTERVAL / 1000}s)`);
  console.log(`Crawl API: x-api-key: ${API_KEY}  →  /v1/web/scrape, /v1/web/crawl, /v1/credits/balance`);
  tick(); // baseline snapshot
});
