// Native mode: the mode probe and the request forwarder, against an in-process fake backend.
import test, { before, after } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { Backend } from '../lib/backend.js';
import { ModeProbe, forward } from '../lib/passthrough.js';

let backendServer, front, base, plans = { status: 200, body: { backend: { enabled: true } } };
const seen = [];

before(async () => {
  backendServer = http.createServer((req, res) => {
    let raw = ''; req.on('data', (d) => (raw += d));
    req.on('end', () => {
      seen.push({ method: req.method, url: req.url, headers: req.headers, body: raw });
      if (req.url === '/api/plans') { res.writeHead(plans.status, { 'content-type': 'application/json' }); return res.end(JSON.stringify(plans.body)); }
      if (req.url === '/api/auth/login') { res.writeHead(200, { 'content-type': 'application/json', 'set-cookie': ['rw_session=abc; HttpOnly; Path=/', 'other=1; Path=/'] }); return res.end('{"redirect":"/app"}'); }
      if (req.url === '/api/limited') { res.writeHead(429, { 'content-type': 'application/json', 'retry-after': '30' }); return res.end('{"error":"Too many attempts"}'); }
      res.writeHead(200, { 'content-type': 'application/json' }); res.end(JSON.stringify({ echoed: req.url }));
    });
  });
  await new Promise((r) => backendServer.listen(0, '127.0.0.1', r));
  const backend = new Backend(`http://127.0.0.1:${backendServer.address().port}`);
  front = http.createServer((req, res) => forward(backend, req, res, new URL(req.url, 'http://x'), { 'x-test': 'base' }).catch((e) => { res.writeHead(500); res.end(e.message); }));
  await new Promise((r) => front.listen(0, '127.0.0.1', r));
  base = `http://127.0.0.1:${front.address().port}`;
});
after(() => { backendServer.close(); front.close(); });

test('ModeProbe: native when the backend answers /api/plans with backend.enabled', async () => {
  const p = new ModeProbe(new Backend(`http://127.0.0.1:${backendServer.address().port}`));
  assert.equal(await p.isNative(), true);
  plans = { status: 404, body: {} }; assert.equal(await p.isNative(), true, 'a positive answer is kept');
  plans = { status: 200, body: { backend: { enabled: true } } };
});

test('ModeProbe: an older backend (404) is the adapter, and is re-checked later', async () => {
  plans = { status: 404, body: { detail: 'Not Found' } };
  const p = new ModeProbe(new Backend(`http://127.0.0.1:${backendServer.address().port}`), 'auto', 30);
  assert.equal(await p.isNative(), false); assert.equal(p.answered, true);
  plans = { status: 200, body: { backend: { enabled: true } } };
  assert.equal(await p.isNative(), false, 'not re-probed inside the retry window');
  await new Promise((r) => setTimeout(r, 40)); assert.equal(await p.isNative(), true);
});

test('ModeProbe: an unreachable backend is retried quickly, and the mode can be forced', async () => {
  const down = new ModeProbe(new Backend('http://127.0.0.1:1'), 'auto', 10_000, 20);
  assert.equal(await down.isNative(), false); assert.equal(down.answered, false);
  assert.equal(await new ModeProbe(new Backend('http://127.0.0.1:1'), 'native').isNative(), true);
  assert.equal(await new ModeProbe(new Backend(`http://127.0.0.1:${backendServer.address().port}`), 'bff').isNative(), false);
  assert.equal(await new ModeProbe(null).isNative(), false, 'no BACKEND_URL: standalone');
});

test('forward: path, query, body, cookie and content type reach the backend; client IP and protocol are added', async () => {
  seen.length = 0;
  const r = await fetch(base + '/api/items?x=1', { method: 'POST', headers: { 'content-type': 'application/json', cookie: 'rw_session=tok' }, body: '{"a":1}' });
  assert.equal(r.status, 200); assert.deepEqual(await r.json(), { echoed: '/api/items?x=1' });
  const s = seen.at(-1);
  assert.equal(s.method, 'POST'); assert.equal(s.body, '{"a":1}'); assert.equal(s.headers.cookie, 'rw_session=tok'); assert.equal(s.headers['content-type'], 'application/json');
  assert.match(s.headers['x-forwarded-for'], /127\.0\.0\.1/); assert.equal(s.headers['x-forwarded-proto'], 'http');
  assert.equal(r.headers.get('x-test'), 'base'); assert.equal(r.headers.get('cache-control'), 'no-store');
});

test('forward: every Set-Cookie and Retry-After come back, statuses are preserved', async () => {
  const r = await fetch(base + '/api/auth/login', { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}' });
  assert.deepEqual(r.headers.getSetCookie(), ['rw_session=abc; HttpOnly; Path=/', 'other=1; Path=/']);
  const l = await fetch(base + '/api/limited'); assert.equal(l.status, 429); assert.equal(l.headers.get('retry-after'), '30');
});

test('forward: an X-Forwarded-For from the client is kept and the proxy hop appended; mutations use their own connection', async () => {
  seen.length = 0;
  await fetch(base + '/api/x', { method: 'POST', headers: { 'content-type': 'application/json', 'x-forwarded-for': '203.0.113.9', 'x-forwarded-proto': 'https' }, body: '{}' });
  const s = seen.at(-1);
  assert.match(s.headers['x-forwarded-for'], /^203\.0\.113\.9, 127\.0\.0\.1/); assert.equal(s.headers['x-forwarded-proto'], 'https');
  assert.equal(s.headers.connection, 'close');
  await fetch(base + '/api/y'); assert.notEqual(seen.at(-1).headers.connection, 'close', 'safe calls reuse pooled connections');
});

test('forward: a request body over 1 MB is refused', async () => {
  const r = await fetch(base + '/api/big', { method: 'POST', headers: { 'content-type': 'application/json' }, body: 'x'.repeat(1_100_000) }).catch(() => null);
  assert.ok(!r || r.status >= 400);
});
