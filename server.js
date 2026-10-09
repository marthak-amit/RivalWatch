import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { makeApi } from './lib/api.js';
import { PLANS, ANNUAL_DISCOUNT, isPlan } from './lib/plans.js';
import { UserStore, Sessions, Limiter, publicUser, normEmail } from './lib/users.js';
import { Workspaces } from './lib/workspace.js';
import * as testsite from './lib/testsite.js';
import { buildReport } from './lib/reports.js';
import { makeDigest } from './lib/digest.js';
import { Backend, BackendError, BackendIdentity, RemoteCrawler, RoutingCrawler, toAdminRow } from './lib/backend.js';

const PORT = +process.env.PORT || 3000;
const HOST = process.env.HOST || '127.0.0.1';
const INTERVAL = (+process.env.CRAWL_INTERVAL_SEC || 300) * 1000;
const ADMIN_PW = process.env.ADMIN_PASSWORD || 'admin1234';
const DEMO_PW = 'demo1234';
const PUBLIC = path.resolve('public');

// BACKEND_URL switches on the Python backend integration (see docs/integration/). Unset = standalone demo.
const BACKEND = process.env.BACKEND_URL ? new Backend(process.env.BACKEND_URL) : null;
const identity = BACKEND ? new BackendIdentity(BACKEND) : null;
// Demo competitors (editable test sites) are on by default standalone, off for real backend accounts.
const DEMO_COMPETITORS = process.env.DEMO_COMPETITORS ? process.env.DEMO_COMPETITORS !== '0' : !BACKEND;
const users = new UserStore(path.resolve(process.env.DATA_DIR || 'data', 'users.json'));
const sessions = new Sessions();
const workspaces = new Workspaces(PORT, { demo: DEMO_COMPETITORS, makeCrawler: BACKEND
  ? (local, ws) => { local.credits = Infinity; return new RoutingCrawler(local, new RemoteCrawler(BACKEND, () => ws.user.apiKey)); } // demo pages local, real sites via the backend
  : undefined });
const authLimiter = new Limiter();
const audit = [];
const log = (actor, action, target, detail = '') => { audit.unshift({ ts: new Date().toISOString(), actor: actor.email, action, target: target?.email ?? '', detail }); audit.length = Math.min(audit.length, 100); };

// ---- seed data (first run only) ------------------------------------------------
if (!BACKEND && !users.list().length) {
  const ago = (d) => new Date(Date.now() - d * 864e5).toISOString();
  users.create({ email: 'admin@rivalwatch.dev', name: 'Ada Admin', password: ADMIN_PW, plan: 'business', role: 'admin', createdAt: ago(60), lastLoginAt: ago(0) });
  users.create({ email: 'demo@rivalwatch.dev', name: 'Dana Demo', password: DEMO_PW, plan: 'pro', createdAt: ago(21), lastLoginAt: ago(1) });
  const rand = () => 'x' + Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2); // seeded customers can't sign in
  [['maria@northwind.example', 'Maria Santos', 'business', 'active', 40, 2], ['li@brightly.example', 'Li Wei', 'pro', 'active', 28, 0],
   ['tom@pixelforge.example', 'Tom Becker', 'starter', 'active', 19, 5], ['priya@loomly.example', 'Priya Nair', 'pro', 'active', 12, 1],
   ['sam@tinyhq.example', 'Sam Ortiz', 'starter', 'suspended', 9, 8], ['ana@cobalt.example', 'Ana Kovac', 'starter', 'active', 3, 0]]
    .forEach(([email, name, plan, status, c, l]) => users.create({ email, name, plan, status, password: rand() + rand(), createdAt: ago(c), lastLoginAt: ago(l) }));
}

// ---- crawl API (per-user keys) + scheduler -------------------------------------
const api = makeApi((key) => { const u = users.byApiKey(key); return u && u.status === 'active' ? workspaces.get(u).crawler : null; });
let nextRun = Date.now() + INTERVAL;
setInterval(() => {
  nextRun = Date.now() + INTERVAL;
  for (const ws of workspaces.all()) if (BACKEND || users.byId(ws.userId)?.status === 'active') ws.monitor.run().catch((e) => console.error('crawl failed', e));
}, INTERVAL);

// ---- http helpers ---------------------------------------------------------------
const HEADERS = {
  'x-content-type-options': 'nosniff', 'x-frame-options': 'DENY', 'referrer-policy': 'same-origin',
  'content-security-policy': "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'",
};
const send = (res, status, body, type = 'application/json', extra = {}) => {
  res.writeHead(status, { ...HEADERS, 'content-type': type, 'cache-control': 'no-store', ...extra });
  res.end(type.startsWith('application/json') ? JSON.stringify(body) : body);
};
const redirect = (res, to) => { res.writeHead(302, { ...HEADERS, location: to }); res.end(); };
const readJson = (req) => new Promise((resolve) => {
  let b = ''; req.on('data', (d) => { b += d; if (b.length > 1e5) req.destroy(); });
  req.on('end', () => { try { resolve(JSON.parse(b || '{}')); } catch { resolve({}); } });
});
const cookie = (req, name) => (req.headers.cookie || '').split(/;\s*/).map((c) => c.split('=')).find(([k]) => k === name)?.[1];
const setSession = (req, token, maxAge = 86400) =>
  ({ 'set-cookie': `rw_session=${token}; HttpOnly; SameSite=Lax; Path=/; Max-Age=${maxAge}${req.headers['x-forwarded-proto'] === 'https' ? '; Secure' : ''}` });
const currentUser = async (req) => {
  if (BACKEND) return identity.resolve(cookie(req, 'rw_session')); // GET /user/me on the backend (briefly cached)
  const id = sessions.get(cookie(req, 'rw_session'));
  const u = id && users.byId(id);
  return u && u.status === 'active' ? u : null;
};
const cleanId = (v) => (/^\w{1,20}$/.test(String(v ?? '')) ? String(v) : ''); // competitor ids are short \w tokens
const fail = (res, status, error) => send(res, status, { error });
const PAGES = { '/': 'index.html', '/login': 'login.html', '/app': 'app.html', '/admin': 'admin.html', '/style.css': 'style.css', '/common.js': 'common.js' };
const TYPES = { '.html': 'text/html; charset=utf-8', '.css': 'text/css', '.js': 'text/javascript' };

const meView = (u) => ({ ...publicUser(u), apiKey: u.apiKey, plan: u.plan, planInfo: PLANS[u.plan], ...(BACKEND ? { credits: u.credits } : {}) });

// ---- routes ---------------------------------------------------------------------
http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://localhost:${PORT}`);
  const p = url.pathname;
  const M = req.method;
  try {
    // pages
    if (M === 'GET' && PAGES[p]) {
      const user = await currentUser(req);
      if (p === '/app' && !user) return redirect(res, '/login');
      if (p === '/admin' && user?.role !== 'admin') return redirect(res, user ? '/app' : '/login');
      const f = PAGES[p];
      return send(res, 200, fs.readFileSync(path.join(PUBLIC, f), 'utf8'), TYPES[path.extname(f)]);
    }

    if (p === '/favicon.ico') { res.writeHead(204, HEADERS); return res.end(); }

    // crawl API
    if (BACKEND && p.startsWith('/v1/')) { // the backend owns the public crawl API (keys, credits, robots.txt, SSRF checks)
      const body = M === 'GET' || M === 'HEAD' ? undefined : await new Promise((ok, no) => { // capped at 1 MB so the proxy can't be used to exhaust memory
        const c = []; let n = 0;
        req.on('data', (d) => { n += d.length; if (n > 1e6) { req.destroy(); no(new BackendError(413, 'Request body too large')); } else c.push(d); });
        req.on('end', () => ok(Buffer.concat(c))); req.on('error', no); });
      const r = await BACKEND.raw(M, p + url.search, { apiKey: req.headers['x-api-key'], body: body?.length ? body : undefined, headers: req.headers['content-type'] ? { 'content-type': req.headers['content-type'] } : {} });
      res.writeHead(r.status, { ...HEADERS, 'content-type': r.type || 'application/json', 'cache-control': 'no-store' });
      return res.end(r.buf);
    }
    if (p.startsWith('/v1/')) {
      const out = await api(M, url, req.headers, M === 'POST' ? await readJson(req) : null);
      return send(res, out.status, out.body);
    }

    // editable fake competitor sites (fetched by the crawler, no cookies)
    const t = p.match(/^\/test\/(\w+)\/(\w+)\/?(.*)$/);
    if (t && M === 'GET') {
      const ws = workspaces.peek(t[1]);
      const html = ws && testsite.render(ws.sites, t[2], t[3].replace(/\/$/, ''), `/test/${t[1]}/${t[2]}`);
      return html ? send(res, 200, html, 'text/html; charset=utf-8') : send(res, 404, 'not found', 'text/plain');
    }

    if (!p.startsWith('/api/')) return send(res, 404, 'not found', 'text/plain');

    // CSRF defence: state-changing calls must be JSON (cross-site forms cannot send that) and SameSite=Lax cookies.
    if (M !== 'GET' && !/^application\/json/.test(req.headers['content-type'] || '')) return fail(res, 415, 'JSON body required');

    // public
    if (p === '/api/session' && M === 'GET') { const u = await currentUser(req); return send(res, 200, { user: u ? meView(u) : null }); }
    if (p === '/api/plans' && M === 'GET') {
      const pair = (v) => { const [email, ...pw] = String(v || '').split(':'); return email && pw.length ? { email, password: pw.join(':') } : null; };
      if (BACKEND) return send(res, 200, { plans: Object.values(PLANS), annualDiscount: ANNUAL_DISCOUNT, demo: { user: pair(process.env.DEMO_USER), admin: pair(process.env.DEMO_ADMIN) },
        // what the backend can't do yet; the UI hides or disables these instead of failing
        backend: { enabled: true, signupName: false, planChanges: false, rotateKey: false, adminPlanEdit: false, adminRoleEdit: false, adminResetCredits: false, adminResetPassword: false } });
      return send(res, 200, { plans: Object.values(PLANS), annualDiscount: ANNUAL_DISCOUNT,
        demo: { user: users.byEmail('demo@rivalwatch.dev') ? { email: 'demo@rivalwatch.dev', password: DEMO_PW } : null,
          admin: !process.env.ADMIN_PASSWORD ? { email: 'admin@rivalwatch.dev', password: ADMIN_PW } : null } });
    }
    if ((p === '/api/auth/login' || p === '/api/auth/signup') && M === 'POST') {
      const b = await readJson(req);
      if (!authLimiter.hit(req.socket.remoteAddress + '|' + normEmail(b.email))) return fail(res, 429, 'Too many attempts. Try again in a few minutes.');
      if (BACKEND) { // one form, both roles; the backend token lives in the HttpOnly cookie
        const email = String(b.email ?? '').trim(), pw = String(b.password ?? '');
        const s = p.endsWith('login') ? await BACKEND.login(email, pw) : await BACKEND.signup(email, pw);
        if (p.endsWith('signup')) log({ email }, 'signup', { email });
        const maxAge = Math.max(60, Math.min(7 * 86400, Math.floor((Date.parse(s.expires_at) - Date.now()) / 1000) || 86400));
        return send(res, 200, { redirect: s.role === 'admin' ? '/admin' : '/app', user: { email: s.email, role: s.role } }, 'application/json', setSession(req, s.token, maxAge));
      }
      let u;
      if (p.endsWith('login')) {
        u = users.authenticate(b.email, b.password);
        if (!u) return fail(res, 401, 'Incorrect email or password');
        if (u.status !== 'active') return fail(res, 403, 'This account is suspended. Contact support.');
      } else {
        const plan = isPlan(b.plan) ? b.plan : 'starter';
        try { u = users.create({ email: b.email, name: b.name, password: b.password, plan }); } catch (e) { return fail(res, 400, e.message); }
        log(u, 'signup', u, `plan ${plan}`);
      }
      users.update(u.id, { lastLoginAt: new Date().toISOString() });
      return send(res, 200, { redirect: u.role === 'admin' ? '/admin' : '/app', user: publicUser(u) }, 'application/json', setSession(req, sessions.create(u.id)));
    }
    if (p === '/api/auth/logout' && M === 'POST') {
      if (BACKEND) { // revoke the session on the backend too, so the token is dead even if it leaked
        const token = cookie(req, 'rw_session'), u = await identity.resolve(token).catch(() => null);
        if (u) await BACKEND.logout(token, u.role).catch(() => {});
        identity.invalidate(token);
        return send(res, 200, { ok: true }, 'application/json', setSession(req, '', 0));
      }
      sessions.destroy(cookie(req, 'rw_session'));
      return send(res, 200, { ok: true }, 'application/json', setSession(req, '', 0));
    }

    // everything below needs a session
    const user = await currentUser(req);
    if (!user) return fail(res, 401, 'Not signed in');

    if (p === '/api/me' && M === 'GET') return send(res, 200, meView(user));
    if (BACKEND && (p === '/api/me/plan' || p === '/api/me/rotate-key') && M === 'POST') return fail(res, 501, 'Not available yet: the backend has no endpoint for this.');
    if (p === '/api/me/plan' && M === 'POST') {
      const { plan } = await readJson(req);
      if (!isPlan(plan)) return fail(res, 400, 'Unknown plan');
      users.update(user.id, { plan });
      const ws = workspaces.peek(user.id); if (ws) ws.crawler.credits = PLANS[plan].credits; // mock checkout: new allowance
      log(user, 'plan_change', user, plan);
      return send(res, 200, meView(user));
    }
    if (p === '/api/me/rotate-key' && M === 'POST') { users.rotateKey(user.id); return send(res, 200, meView(user)); }

    // dashboard
    if (p.startsWith('/api/') && !p.startsWith('/api/admin')) {
      const ws = workspaces.get(user);
      ws.user = user; // the remote crawler reads the current API key from here
      const plan = PLANS[user.plan];
      if (p === '/api/state' && M === 'GET') {
        const view = ws.monitor.view();
        return send(res, 200, { ...view, credits: BACKEND ? user.credits : ws.crawler.credits, plan, nextRun, intervalSec: INTERVAL / 1000,
          mode: process.env.ANTHROPIC_API_KEY ? 'claude' : 'rules',
          testSites: view.competitors.filter((c) => c.site).map((c) => ({ id: c.site, name: c.name, url: `/test/${user.id}/${c.site}/` })) });
      }
      if (p === '/api/crawl' && M === 'POST') { const r = await ws.monitor.run(); if (BACKEND) identity.invalidate(user.token); /* credits changed */ return send(res, 200, { skipped: r.skipped, changes: r.changes.length }); }
      if (p === '/api/competitors' && M === 'POST') {
        const b = await readJson(req);
        let u; try { u = new URL(String(b.url || '')); if (!/^https?:$/.test(u.protocol)) throw 0; } catch { return fail(res, 400, 'Enter a valid http(s) URL'); }
        if (ws.monitor.competitors.length >= plan.competitors) return fail(res, 403, `Your ${plan.name} plan allows ${plan.competitors} competitors. Upgrade to add more.`);
        if (ws.monitor.competitors.some((c) => c.url === u.href)) return fail(res, 400, 'Already monitoring this URL');
        const c = { id: 'u' + Math.random().toString(36).slice(2, 8), name: String(b.name || '').trim().slice(0, 40) || u.hostname, url: u.href };
        ws.monitor.add(c);
        ws.monitor.run([c.id]).then(() => BACKEND && identity.invalidate(user.token)).catch(() => {}); // baseline for the new competitor only
        return send(res, 200, { ok: true });
      }
      const dc = p.match(/^\/api\/competitors\/(\w+)$/);
      if (dc && M === 'PATCH') {
        const { enabled } = await readJson(req);
        if (typeof enabled !== 'boolean') return fail(res, 400, '`enabled` must be true or false');
        if (!ws.monitor.setEnabled(dc[1], enabled)) return fail(res, 404, 'Competitor not found');
        if (enabled) ws.monitor.run([dc[1]]).catch(() => {}); // catch up on anything that changed while paused
        return send(res, 200, { ok: true, enabled });
      }
      if (dc && M === 'DELETE') { ws.monitor.remove(dc[1]); return send(res, 200, { ok: true }); }
      if (p === '/api/reports' && M === 'GET') {
        return send(res, 200, buildReport(ws.monitor.state, url.searchParams.get('period') === 'month' ? 'month' : 'week', undefined, cleanId(url.searchParams.get('competitor'))));
      }
      if (p === '/api/reports/summary' && M === 'POST') {
        const { period, competitor } = await readJson(req);
        const comp = cleanId(competitor);
        const r = buildReport(ws.monitor.state, period === 'month' ? 'month' : 'week', undefined, comp);
        if (!r.total) return fail(res, 400, 'No changes in this period to summarise');
        const inRange = ws.monitor.state.changes.filter((c) => c.ts.slice(0, 10) >= r.from && (!comp || c.competitorId === comp));
        return send(res, 200, await makeDigest(inRange));
      }
      if (p === '/api/test/edit' && M === 'POST') {
        const { site, action } = await readJson(req);
        return send(res, 200, { edited: testsite.edit(ws.sites, site, action) });
      }
      if (p === '/api/test/reset' && M === 'POST') {
        testsite.reset(ws.sites);
        const missing = workspaces.demoCompetitors(user, ws.sites).filter((c) => !ws.monitor.competitors.some((x) => x.id === c.id));
        let added = 0;
        for (const c of missing) { if (ws.monitor.competitors.length >= plan.competitors) break; ws.monitor.add(c); added++; }
        if (missing.length && !added) return fail(res, 403, `Your ${plan.name} plan allows ${plan.competitors} competitors. Remove one to load the demo competitors.`);
        const real = ws.monitor.competitors.filter((c) => !c.site).map((c) => c.id); // real competitors keep their history
        ws.monitor.reset(real); ws.monitor.seedHistory();
        if (!BACKEND) ws.crawler.credits = plan.credits;
        await ws.monitor.run(ws.monitor.competitors.filter((c) => c.site).map((c) => c.id)); // re-baseline the demo sites only
        return send(res, 200, { ok: true });
      }
      return fail(res, 404, 'not found');
    }

    // admin
    if (user.role !== 'admin') return fail(res, 403, 'Admin only');
    if (BACKEND) { // user management goes to the backend's /admin/users; what it can't do yet answers 501
      const tok = user.token;
      const NOPE = 'Not supported by the backend yet.';
      const rows = async () => (await BACKEND.listUsers(tok)).map((u) => toAdminRow(u, workspaces.peek(String(u.id))?.monitor.competitors.length ?? null));
      const find = async (id) => (await rows()).find((r) => r.id === String(id));
      if (p === '/api/admin/stats' && M === 'GET') {
        const all = await BACKEND.listUsers(tok);
        return send(res, 200, { total: all.length, active: all.filter((u) => u.is_active).length, suspended: all.filter((u) => !u.is_active).length,
          admins: all.filter((u) => u.role === 'admin').length, activeNow: all.filter((u) => u.active_now).length, byPlan: null, mrr: null,
          newThisWeek: all.filter((u) => Date.now() - Date.parse(u.created_at) < 7 * 864e5).length });
      }
      if (p === '/api/admin/users' && M === 'GET') {
        const q = (url.searchParams.get('q') || '').toLowerCase(), st = url.searchParams.get('status');
        const out = (await rows()).filter((r) => (!q || r.email.toLowerCase().includes(q) || r.name.toLowerCase().includes(q)) && (!st || r.status === st))
          .sort((a, b) => String(b.createdAt).localeCompare(String(a.createdAt)));
        return send(res, 200, { users: out });
      }
      if (p === '/api/admin/users' && M === 'POST') {
        const b = await readJson(req);
        if (b.role === 'admin') return fail(res, 403, 'Admins can only be created directly in the database.');
        const s = await BACKEND.signup(String(b.email ?? '').trim(), String(b.password ?? ''));
        await BACKEND.logout(s.token, 'user').catch(() => {}); // signup opens a session for the new user; close it
        log(user, 'create_user', { email: s.email }, 'user');
        return send(res, 200, await find(s.id));
      }
      const bu = p.match(/^\/api\/admin\/users\/(\w+)(?:\/(reset-credits|reset-password))?$/);
      if (bu) {
        const [, id, action] = bu;
        if (action) return fail(res, 501, NOPE);
        const target = await find(id);
        if (!target) return fail(res, 404, 'User not found');
        if (M === 'PATCH') {
          const b = await readJson(req);
          if (b.plan !== undefined || b.role !== undefined) return fail(res, 501, NOPE);
          if (!['active', 'suspended'].includes(b.status)) return fail(res, 400, 'Bad status');
          if (id === user.id) return fail(res, 400, "You can't suspend yourself");
          await BACKEND.setActive(tok, id, b.status === 'active'); // also ends that user's sessions on the backend
          identity.invalidateUser(id);
          log(user, 'update_user', target, JSON.stringify({ status: b.status }));
          return send(res, 200, await find(id));
        }
        if (M === 'DELETE') {
          if (id === user.id) return fail(res, 400, "You can't delete your own account");
          await BACKEND.deleteUser(tok, id);
          workspaces.drop(id); identity.invalidateUser(id);
          log(user, 'delete_user', target);
          return send(res, 200, { ok: true });
        }
      }
      if (p === '/api/admin/audit' && M === 'GET') return send(res, 200, { audit });
      return fail(res, 404, 'not found');
    }
    const row = (u) => ({ ...publicUser(u), credits: workspaces.peek(u.id)?.crawler.credits ?? PLANS[u.plan].credits,
      creditLimit: PLANS[u.plan].credits, competitors: workspaces.peek(u.id)?.monitor.competitors.length ?? 3 });
    const activeAdmins = () => users.list().filter((u) => u.role === 'admin' && u.status === 'active');

    if (p === '/api/admin/stats' && M === 'GET') {
      const all = users.list();
      const byPlan = Object.fromEntries(Object.keys(PLANS).map((k) => [k, all.filter((u) => u.plan === k && u.status === 'active' && u.role === 'user').length]));
      return send(res, 200, { total: all.length, active: all.filter((u) => u.status === 'active').length, suspended: all.filter((u) => u.status === 'suspended').length,
        admins: all.filter((u) => u.role === 'admin').length, byPlan, mrr: Object.entries(byPlan).reduce((s, [k, n]) => s + n * PLANS[k].price, 0),
        newThisWeek: all.filter((u) => Date.now() - Date.parse(u.createdAt) < 7 * 864e5).length });
    }
    if (p === '/api/admin/users' && M === 'GET') {
      const q = (url.searchParams.get('q') || '').toLowerCase();
      const out = users.list().filter((u) => (!q || u.email.includes(q) || u.name.toLowerCase().includes(q))
        && (!url.searchParams.get('plan') || u.plan === url.searchParams.get('plan'))
        && (!url.searchParams.get('status') || u.status === url.searchParams.get('status')))
        .sort((a, b) => b.createdAt.localeCompare(a.createdAt)).map(row);
      return send(res, 200, { users: out });
    }
    if (p === '/api/admin/users' && M === 'POST') {
      const b = await readJson(req);
      try {
        const u = users.create({ email: b.email, name: b.name, password: b.password, plan: isPlan(b.plan) ? b.plan : 'starter', role: b.role === 'admin' ? 'admin' : 'user' });
        log(user, 'create_user', u, `${u.role}, ${u.plan}`);
        return send(res, 200, row(u));
      } catch (e) { return fail(res, 400, e.message); }
    }
    const au = p.match(/^\/api\/admin\/users\/(\w+)(?:\/(reset-credits|reset-password))?$/);
    if (au) {
      const target = users.byId(au[1]);
      if (!target) return fail(res, 404, 'User not found');
      const self = target.id === user.id;
      if (M === 'PATCH' && !au[2]) {
        const b = await readJson(req); const patch = {};
        if (b.plan !== undefined) { if (!isPlan(b.plan)) return fail(res, 400, 'Unknown plan'); patch.plan = b.plan; }
        if (b.status !== undefined) { if (!['active', 'suspended'].includes(b.status)) return fail(res, 400, 'Bad status'); patch.status = b.status; }
        if (b.role !== undefined) { if (!['user', 'admin'].includes(b.role)) return fail(res, 400, 'Bad role'); patch.role = b.role; }
        const losesAdmin = target.role === 'admin' && (patch.role === 'user' || patch.status === 'suspended');
        if (losesAdmin && (self || activeAdmins().length <= 1)) return fail(res, 400, self ? "You can't demote or suspend yourself" : 'At least one active admin is required');
        users.update(target.id, patch);
        if (patch.status === 'suspended') sessions.destroyForUser(target.id);
        if (patch.plan) { const ws = workspaces.peek(target.id); if (ws) ws.crawler.credits = PLANS[patch.plan].credits; }
        log(user, 'update_user', target, JSON.stringify(patch));
        return send(res, 200, row(target));
      }
      if (M === 'POST' && au[2] === 'reset-credits') {
        const ws = workspaces.peek(target.id); if (ws) ws.crawler.credits = PLANS[target.plan].credits;
        log(user, 'reset_credits', target); return send(res, 200, row(target));
      }
      if (M === 'POST' && au[2] === 'reset-password') {
        const { password } = await readJson(req);
        try { users.setPassword(target.id, password); } catch (e) { return fail(res, 400, e.message); }
        sessions.destroyForUser(target.id); log(user, 'reset_password', target);
        return send(res, 200, { ok: true });
      }
      if (M === 'DELETE' && !au[2]) {
        if (self) return fail(res, 400, "You can't delete your own account");
        if (target.role === 'admin' && activeAdmins().length <= 1) return fail(res, 400, 'At least one active admin is required');
        users.remove(target.id); workspaces.drop(target.id); sessions.destroyForUser(target.id);
        log(user, 'delete_user', target); return send(res, 200, { ok: true });
      }
    }
    if (p === '/api/admin/audit' && M === 'GET') return send(res, 200, { audit });
    fail(res, 404, 'not found');
  } catch (e) {
    if (e instanceof BackendError) return fail(res, e.status, e.message); // backend said no (or is down)
    console.error(e);
    fail(res, 400, e.message);
  }
}).listen(PORT, HOST, () => {
  console.log(`RivalWatch on http://${HOST === '127.0.0.1' ? 'localhost' : HOST}:${PORT}  (digest: ${process.env.ANTHROPIC_API_KEY ? 'Claude' : 'rule-based'}, crawl every ${INTERVAL / 1000}s)`);
  if (BACKEND) console.log(`Backend: ${process.env.BACKEND_URL}  (accounts, sessions, admin users and the /v1 crawl API; demo competitors ${DEMO_COMPETITORS ? 'on' : 'off'})`);
  else console.log(`Demo logins: demo@rivalwatch.dev / ${DEMO_PW}   admin@rivalwatch.dev / ${process.env.ADMIN_PASSWORD ? '(ADMIN_PASSWORD)' : ADMIN_PW}`);
});
