// Unit tests for the backend integration layer, against an in-process fake that mimics the real backend's
// response shapes (observed from backend/src: FastAPI `detail` errors, /v1 envelopes, async crawl jobs).
import test, { before, after } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { Backend, BackendError, BackendIdentity, RemoteCrawler, RoutingCrawler, errorMessage, toAdminRow, toUser } from '../lib/backend.js';

const calls = [];
let polls = 0, server, base;
const json = (res, status, body) => { res.writeHead(status, { 'content-type': 'application/json' }); res.end(JSON.stringify(body)); };
const ME = { id: 1, email: 'user@x.test', role: 'user', name: null, plan: 'pro', credits: 12, api_key: 'rw_k', last_login_at: null };

before(async () => {
  server = http.createServer((req, res) => {
    let raw = ''; req.on('data', (d) => (raw += d));
    req.on('end', () => {
      const b = raw ? JSON.parse(raw) : {}; const url = new URL(req.url, 'http://x'); calls.push(`${req.method} ${url.pathname}`);
      const bearer = req.headers.authorization, key = req.headers['x-api-key'];
      if (url.pathname === '/user/login') return b.email === 'user@x.test' && b.password === 'pw12345678' ? json(res, 200, { token: 'U1', expires_at: '2030-01-01T00:00:00+00:00', role: 'user', email: b.email }) : json(res, 401, { detail: 'Invalid credentials' });
      if (url.pathname === '/admin/login') return b.email === 'boss@x.test' && b.password === 'adminpass1' ? json(res, 200, { token: 'A1', expires_at: '2030-01-01T00:00:00+00:00', role: 'admin', email: b.email }) : json(res, 401, { detail: 'Invalid credentials' });
      if (url.pathname === '/user/signup') {
        if (b.email === 'taken@x.test') return json(res, 409, { detail: 'Email already registered' });
        if (!/@/.test(b.email)) return json(res, 422, { detail: [{ type: 'string_pattern_mismatch', loc: ['body', 'email'], msg: 'String should match pattern' }] });
        return json(res, 201, { id: 7, email: b.email, role: 'user', token: 'N1', expires_at: '2030-01-01T00:00:00+00:00' });
      }
      if (url.pathname === '/user/me') return bearer === 'Bearer U1' ? json(res, 200, ME) : json(res, 401, { detail: 'Invalid or expired token' });
      if (url.pathname.startsWith('/v1/')) {
        if (key !== 'rw_k') return json(res, 401, { success: false, error: { type: 'auth_error', message: 'Missing or invalid x-api-key header', status: 401 } });
        if (req.method === 'POST') return b.url.includes('blocked') ? json(res, 400, { success: false, error: { type: 'request_error', message: 'private/internal addresses are blocked', status: 400 } })
          : json(res, 202, { success: true, data: { job_id: b.url.includes('slow') ? 'crawl_slow' : 'crawl_1', status: 'queued' } });
        if (url.pathname.endsWith('crawl_slow')) return json(res, 200, { success: true, data: { status: 'running', progress: { completed: 0, total: 5 } } });
        polls++; return json(res, 200, { success: true, data: polls < 3 ? { status: 'running', progress: { completed: polls, total: 3 } }
          : { status: 'completed', error: null, progress: { completed: 3, total: 3 }, result: { pages: [{ url: 'http://r.test/', title: 'R', markdown: '# R', links: [] }] } } });
      }
      json(res, 404, { detail: 'Not Found' });
    });
  }).listen(0, '127.0.0.1');
  await new Promise((r) => server.once('listening', r));
  base = `http://127.0.0.1:${server.address().port}`;
});
after(() => server.close());

test('errorMessage turns FastAPI and crawl-API errors into one readable message', () => {
  assert.equal(errorMessage({ detail: 'Email already registered' }, 409), 'Email already registered');
  assert.equal(errorMessage({ detail: [{ loc: ['body', 'email'], msg: 'x' }] }, 422), 'Enter a valid email address');
  assert.equal(errorMessage({ detail: [{ loc: ['body', 'password'], msg: 'x' }] }, 422), 'Password must be at least 8 characters');
  assert.equal(errorMessage({ success: false, error: { message: 'insufficient credits' } }, 402), 'insufficient credits');
  assert.equal(errorMessage(null, 500), 'Backend error (500)');
});

test('login tries the user login first, then the admin login, in that order', async () => {
  const b = new Backend(base);
  calls.length = 0; assert.equal((await b.login('user@x.test', 'pw12345678')).role, 'user'); assert.deepEqual(calls, ['POST /user/login']);
  calls.length = 0; assert.equal((await b.login('boss@x.test', 'adminpass1')).role, 'admin'); assert.deepEqual(calls, ['POST /user/login', 'POST /admin/login']);
  await assert.rejects(b.login('user@x.test', 'wrong'), (e) => e instanceof BackendError && e.status === 401 && /Incorrect email or password/.test(e.message));
});

test('signup maps 409 and 422 to friendly messages', async () => {
  const b = new Backend(base);
  assert.equal((await b.signup('new@x.test', 'pw12345678')).id, 7);
  await assert.rejects(b.signup('taken@x.test', 'pw12345678'), (e) => e.status === 409 && /already exists/.test(e.message));
  await assert.rejects(b.signup('nope', 'pw12345678'), (e) => e.status === 400 && /valid email/.test(e.message));
});

test('me() is null for a bad token, and an unreachable backend is a 503 BackendError', async () => {
  const b = new Backend(base);
  assert.equal(await b.me('nope'), null); assert.equal((await b.me('U1')).email, 'user@x.test');
  await assert.rejects(new Backend('http://127.0.0.1:1').me('U1'), (e) => e instanceof BackendError && e.status === 503);
});

test('identity: maps the account, derives a name, caches briefly, and can be invalidated', async () => {
  const b = new Backend(base), id = new BackendIdentity(b, 60_000);
  calls.length = 0;
  const u = await id.resolve('U1'); await id.resolve('U1');
  assert.equal(calls.filter((c) => c === 'GET /user/me').length, 1, 'second resolve is served from the cache');
  assert.deepEqual({ id: u.id, name: u.name, plan: u.plan, credits: u.credits, apiKey: u.apiKey, status: u.status }, { id: '1', name: 'User', plan: 'pro', credits: 12, apiKey: 'rw_k', status: 'active' });
  id.invalidate('U1'); await id.resolve('U1'); assert.equal(calls.filter((c) => c === 'GET /user/me').length, 2);
  assert.equal(await id.resolve('bad'), null); assert.equal(await id.resolve(undefined), null);
  assert.equal(toUser({ ...ME, plan: 'mystery' }).plan, 'starter', 'unknown plans fall back to starter');
});

test('toAdminRow marks fields the backend does not have yet as null', () => {
  const r = toAdminRow({ id: 5, email: 'ana.kovac@x.test', role: 'user', is_active: false, created_at: 'c', last_login_at: null, active_now: true, session_count: 2, total_session_seconds: 90 }, 3);
  assert.deepEqual(r, { id: '5', email: 'ana.kovac@x.test', role: 'user', status: 'suspended', name: 'Ana Kovac', plan: null, credits: null, creditLimit: null, competitors: 3,
    createdAt: 'c', lastLoginAt: null, activeNow: true, sessions: 2, totalSessionSec: 90 });
});

test('RemoteCrawler starts a job, polls it to completion, and surfaces API errors', async () => {
  const rc = new RemoteCrawler(new Backend(base), () => 'rw_k');
  const job = await rc.startCrawl('http://r.test/', { limit: 8, max_depth: 1 }); assert.equal(job.job_id, 'crawl_1');
  polls = 0; const done = await rc.waitForJob(job.job_id, 10_000);
  assert.equal(done.status, 'completed'); assert.equal(done.result.pages[0].title, 'R'); assert.ok(polls >= 3, 'polled until finished');
  await assert.rejects(rc.startCrawl('http://blocked.test/'), /private\/internal addresses are blocked/);
  await assert.rejects(new RemoteCrawler(new Backend(base), () => 'wrong').startCrawl('http://r.test/'), /x-api-key/);
  await assert.rejects(rc.waitForJob('crawl_slow', 600), /worker running/);
});

test('RoutingCrawler keeps demo pages local and sends everything else to the backend', async () => {
  const seen = [];
  const stub = (name) => ({ startCrawl: async (u) => { seen.push(`${name}:${u}`); return { job_id: `${name}_job` }; }, waitForJob: async (id) => ({ status: 'completed', from: id }), credits: 5, allowLocal: (u) => u.hostname === 'localhost' });
  const rc = new RoutingCrawler(stub('local'), stub('remote'));
  const a = await rc.startCrawl('http://localhost:3000/test/1/acme/'); const b = await rc.startCrawl('https://rival.example/');
  assert.deepEqual(seen, ['local:http://localhost:3000/test/1/acme/', 'remote:https://rival.example/']);
  assert.equal((await rc.waitForJob(a.job_id)).from, 'local_job'); assert.equal((await rc.waitForJob(b.job_id)).from, 'remote_job');
});

import { Workspaces } from '../lib/workspace.js';
test('workspaces hold only real data by default; sample history is strictly opt-in', () => {
  const user = { id: 'u1', plan: 'starter', apiKey: 'k' };
  const real = new Workspaces(3000, { demo: false }).get(user);
  assert.equal(real.monitor.competitors.length, 0); assert.equal(real.monitor.state.changes.length, 0); assert.equal(real.monitor.state.crawlLog.filter((r) => r.sample).length, 0);
  const demo = new Workspaces(3000, { demo: true, sampleHistory: true }).get({ ...user, id: 'u2' });
  assert.ok(demo.monitor.state.changes.length > 0 && demo.monitor.state.changes.every((c) => c.sample), 'sample rows exist only when asked for, and are flagged');
  const noSample = new Workspaces(3000, { demo: true }).get({ ...user, id: 'u3' });
  assert.equal(noSample.monitor.state.changes.length, 0, 'demo competitors alone do not bring fabricated history');
});

import { credentialProblem } from '../lib/backend.js';
import { Limiter } from '../lib/users.js';

test('credentialProblem keeps control characters and oversized input away from the backend', () => {
  assert.equal(credentialProblem('a@b.co', 'password123'), null);
  assert.match(credentialProblem('nul\u0000l@x.com', 'pw'), /invalid characters/);
  assert.match(credentialProblem('a@b.co', 'pw\u0007'), /invalid characters/);
  assert.match(credentialProblem(['x'], { a: 1 }), /Enter your email/);
  assert.equal(credentialProblem('a'.repeat(300) + '@x.com', 'p'), 'Incorrect email or password', 'login does not hint at limits');
  assert.match(credentialProblem('a@b.co', 'p'.repeat(200), { forSignup: true }), /too long/);
});

test('Backend retries a stale pooled connection once, but only for calls that are safe to repeat', async () => {
  const real = globalThis.fetch; let calls = 0;
  const staleThenOk = (code) => async () => { calls++; if (calls === 1) throw Object.assign(new TypeError('fetch failed'), { cause: { code } }); return new Response('{"ok":true}', { status: 200, headers: { 'content-type': 'application/json' } }); };
  try {
    const b = new Backend('http://backend.test');
    calls = 0; globalThis.fetch = staleThenOk('UND_ERR_SOCKET'); assert.equal((await b.json('GET', '/x')).status, 200); assert.equal(calls, 2, 'GET retried after a stale socket');
    calls = 0; globalThis.fetch = staleThenOk('ECONNRESET'); assert.equal((await b.json('POST', '/user/login', { body: {}, retry: true })).status, 200); assert.equal(calls, 2, 'login retried');
    calls = 0; globalThis.fetch = staleThenOk('ECONNRESET'); await assert.rejects(b.json('POST', '/user/signup', { body: {} }), (e) => e.status === 503); assert.equal(calls, 1, 'signup is NOT retried (could double-apply)');
    calls = 0; globalThis.fetch = staleThenOk('ECONNREFUSED'); await assert.rejects(b.json('GET', '/x'), (e) => e.status === 503); assert.equal(calls, 1, 'a refused connection (backend down) is not retried');
    calls = 0; globalThis.fetch = async () => { calls++; throw Object.assign(new TypeError('fetch failed'), { cause: { code: 'ECONNRESET' } }); };
    await assert.rejects(b.json('GET', '/x'), (e) => e.status === 503); assert.equal(calls, 2, 'gives up after one retry');
  } finally { globalThis.fetch = real; }
});

test('rate limiter: failures add up, a success forgets them', () => {
  const l = new Limiter(3, 60_000);
  assert.ok(l.hit('k') && l.hit('k') && l.hit('k')); assert.equal(l.hit('k'), false, '4th attempt is blocked');
  l.reset('k'); assert.ok(l.hit('k'), 'reset after a successful login');
  assert.ok(l.hit('other'), 'other keys are independent');
});
