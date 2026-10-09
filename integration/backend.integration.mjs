// Integration check: this Node server (BACKEND_URL set) <-> the real Python backend (Postgres + crawl worker).
// Not part of `npm test` (needs live services). See integration/README.md for how to start them.
import './rival.mjs'; // stand-in "real" competitor website on 127.0.0.1:3999
const N = process.env.NODE_URL || 'http://127.0.0.1:3000', RIVAL = 'http://127.0.0.1:3999';
const ADMIN = { email: process.env.ADMIN_EMAIL || 'boss@rivalwatch.test', password: process.env.ADMIN_PASSWORD || 'admin-pass-12345' };
let bad = 0; const ok = (c, m) => { console.log(c ? 'ok  ' : 'FAIL', m); if (!c) bad++; };
const jar = () => { let ck = ''; return async (m, p, b, extra = {}) => {
  const r = await fetch(N + p, { method: m, headers: { ...(m === 'GET' ? {} : { 'content-type': 'application/json' }), cookie: ck, ...extra }, body: m === 'GET' || b === undefined ? undefined : JSON.stringify(b), redirect: 'manual' });
  const sc = r.headers.get('set-cookie'); if (sc) ck = sc.split(';')[0].startsWith('rw_session=;') ? '' : sc.split(';')[0];
  const t = await r.text(); let d; try { d = JSON.parse(t); } catch { d = t; } return { status: r.status, data: d, headers: r.headers, cookie: () => ck }; }; };
const wait = async (fn, ms = 20000) => { const t0 = Date.now(); for (;;) { const v = await fn(); if (v) return v; if (Date.now() - t0 > ms) return null; await new Promise((r) => setTimeout(r, 400)); } };

const email = `it${Date.now()}@example.com`;
const u = jar();
// --- auth
let r = await u('POST', '/api/auth/signup', { email: 'nope', password: 'x' }); ok(r.status === 400 && /valid email/i.test(r.data.error), 'signup validation message: ' + r.data.error);
r = await u('POST', '/api/auth/signup', { email, password: 'password123', name: 'Ignored', plan: 'business' }); ok(r.status === 200 && r.data.redirect === '/app', 'signup -> /app');
ok(/HttpOnly/i.test(r.headers.get('set-cookie')) && /SameSite=Lax/i.test(r.headers.get('set-cookie')), 'session cookie is HttpOnly + SameSite=Lax');
ok(!JSON.stringify(r.data).includes('eyJ'), 'token is not in the response body');
const dup = await jar()('POST', '/api/auth/signup', { email, password: 'password123' }); ok(dup.status === 409 && /already exists/.test(dup.data.error), 'duplicate signup -> 409');
r = await u('GET', '/api/me'); const me = r.data; ok(r.status === 200 && me.email === email && me.plan === 'starter' && me.credits === 300 && /^rw_/.test(me.apiKey), `/api/me from backend (plan ${me.plan}, credits ${me.credits}, key ${me.apiKey?.slice(0, 7)}…)`);
ok(!('passwordHash' in me) && !('token' in me), '/api/me leaks no secrets');
r = await u('GET', '/api/session'); ok(r.data.user?.email === email, '/api/session returns the user');
r = await u('POST', '/api/me/plan', { plan: 'pro' }); ok(r.status === 501, 'plan change -> 501 (backend has no endpoint yet)');
r = await u('POST', '/api/me/rotate-key', {}); ok(r.status === 501, 'rotate key -> 501');
r = await jar()('POST', '/api/auth/login', { email, password: 'wrong-password' }); ok(r.status === 401 && /Incorrect/.test(r.data.error), 'bad password -> 401');
r = await u('GET', '/api/admin/users'); ok(r.status === 403, 'normal user is forbidden from admin API');
r = await jar()('GET', '/api/state'); ok(r.status === 401, 'no cookie -> 401');
r = await fetch(N + '/app', { redirect: 'manual' }); ok(r.status === 302, '/app redirects when signed out');

// --- empty workspace by default (no fake data for real accounts)
let st = (await u('GET', '/api/state')).data; ok(st.competitors.length === 0 && st.credits === 300 && st.plan.id === 'starter', 'new account: empty workspace, 300 credits');

// --- real competitor crawled through the backend
r = await u('POST', '/api/competitors', { name: 'Rival Co', url: RIVAL + '/' }); ok(r.status === 200, 'add competitor');
st = await wait(async () => { const s = (await u('GET', '/api/state')).data; return s.competitors[0]?.snapshot ? s : null; });
const c = st?.competitors[0]; ok(!!c, 'baseline snapshot arrives via the backend crawl worker');
ok(c?.snapshot.pagesCrawled === 3 && Object.keys(c.snapshot.products).sort().join() === 'widget max,widget mini', 'pages + products extracted from backend crawl result');
ok(c?.snapshot.profile?.headline === 'Widgets for busy teams' && c.snapshot.profile.emails[0] === 'hi@rival.example' && c.snapshot.profile.socials[0]?.network === 'X', 'company profile extracted (headline, email, social)');
ok(c?.snapshot.pagesCrawled === 3 && !c.snapshot.profile.keyPages.some((k) => k.path.includes('private')), 'robots.txt-disallowed page was linked but never fetched (3 pages crawled)');
st = (await u('GET', '/api/state')).data; ok(st.credits === 297, `backend credits charged for the crawl (300 -> ${st.credits})`);
await new Promise((x) => setTimeout(x, 4500)); // identity cache TTL
r = await u('GET', '/api/me'); ok(r.data.credits === 297, '/api/me shows the same balance');

// --- change detection through the backend crawler
await fetch(RIVAL + '/__edit', { method: 'POST' });
r = await u('POST', '/api/crawl'); ok(r.status === 200 && r.data.changes === 1, 'crawl now detected 1 change');
st = (await u('GET', '/api/state')).data; const ch = st.changes[0];
ok(ch?.type === 'price_change' && ch.name === 'Widget Mini' && ch.from === '$12/mo' && ch.to === '$9/mo' && ch.pct === -25, `price drop detected (${ch?.label})`);
ok(st.digest?.actions.length >= 3, 'digest generated with ≥3 actions');
ok(st.credits === 294, `credits charged again (297 -> ${st.credits})`);

// --- error paths surface on the card
await u('POST', '/api/competitors', { name: 'Blocked', url: RIVAL + '/private/secret' });
await u('POST', '/api/competitors', { name: 'Metadata', url: 'http://169.254.169.254/latest/' });
st = await wait(async () => { const s = (await u('GET', '/api/state')).data; const e = s.competitors.filter((x) => x.error); return e.length === 2 ? s : null; });
ok(/robots/i.test(st?.competitors.find((x) => x.name === 'Blocked')?.error || ''), 'robots.txt block shown: ' + st?.competitors.find((x) => x.name === 'Blocked')?.error);
ok(/private|internal|blocked/i.test(st?.competitors.find((x) => x.name === 'Metadata')?.error || ''), 'SSRF block shown: ' + st?.competitors.find((x) => x.name === 'Metadata')?.error);
r = await u('POST', '/api/competitors', { name: 'Fourth', url: 'https://example.org/' }); ok(r.status === 403 && /Starter/.test(r.data.error), 'plan limit enforced (3 on Starter)');

// --- /v1 is the backend's public API, proxied
const key = me.apiKey;
r = await fetch(N + '/v1/credits/balance', { headers: { 'x-api-key': key } }); let j = await r.json(); ok(r.status === 200 && j.success && j.data.balance === st.credits, 'GET /v1/credits/balance via proxy');
r = await fetch(N + `/v1/web/scrape?url=${encodeURIComponent(RIVAL + '/pricing')}`, { headers: { 'x-api-key': key } }); j = await r.json(); ok(j.success && /Widget Max/.test(j.data.markdown) && j.credits_used === 1, 'GET /v1/web/scrape via proxy (1 credit)');
r = await fetch(N + '/v1/credits/balance', { headers: { 'x-api-key': 'rw_bogus' } }); j = await r.json(); ok(r.status === 401 && j.error?.type === 'auth_error', '/v1 with a bad key -> 401 envelope');
r = await fetch(N + '/v1/web/crawl', { method: 'POST', headers: { 'x-api-key': key, 'content-type': 'application/json' }, body: JSON.stringify({ url: RIVAL + '/', limit: 2, max_depth: 0 }) }); j = await r.json(); ok(r.status === 202 && j.data.job_id, 'POST /v1/web/crawl via proxy -> 202 job');

// --- demo competitors are opt-in and respect the plan limit
r = await u('POST', '/api/test/reset', {}); ok(r.status === 403 && /Remove one/.test(r.data.error), 'load demo competitors refused while at the plan limit');
for (const id of st.competitors.filter((x) => x.error).map((x) => x.id)) await u('DELETE', '/api/competitors/' + id);
r = await u('POST', '/api/test/reset', {}); st = (await u('GET', '/api/state')).data; ok(r.status === 200 && st.competitors.some((x) => x.site) && st.competitors.some((x) => !x.site), 'demo competitors added next to the real one');
ok(st.competitors.find((x) => x.name === 'Rival Co')?.snapshot && st.changes.some((x) => x.competitor === 'Rival Co'), 'reset kept the real competitor\'s snapshot and history');

// --- logout revokes the token on the backend
r = await u('POST', '/api/auth/logout', {}); ok(r.status === 200, 'logout');
r = await u('GET', '/api/me'); ok(r.status === 401, 'session gone after logout');

// --- admin through the BFF
const a = jar();
r = await a('POST', '/api/auth/login', ADMIN); ok(r.status === 200 && r.data.redirect === '/admin', 'admin login -> /admin (one form, both roles)');
r = await a('GET', '/api/admin/stats'); ok(r.data.total >= 3 && r.data.admins >= 1 && r.data.byPlan === null, `admin stats from backend (${r.data.total} users, ${r.data.admins} admin)`);
r = await a('GET', '/api/admin/users?q=' + encodeURIComponent(email)); const row = r.data.users?.[0]; ok(row?.email === email && row.status === 'active' && row.role === 'user' && row.plan === null && typeof row.sessions === 'number', 'admin user list mapped (plan/credits null until backend has them)');
r = await a('PATCH', '/api/admin/users/' + row.id, { plan: 'pro' }); ok(r.status === 501, 'admin plan edit -> 501');
r = await a('POST', '/api/admin/users/' + row.id + '/reset-credits', {}); ok(r.status === 501, 'admin reset credits -> 501');
const victim = jar(); await victim('POST', '/api/auth/login', { email, password: 'password123' }); ok((await victim('GET', '/api/me')).status === 200, 'victim signed in again');
r = await a('PATCH', '/api/admin/users/' + row.id, { status: 'suspended' }); ok(r.status === 200 && r.data.status === 'suspended', 'admin suspends user');
ok((await victim('GET', '/api/me')).status === 401, 'suspended user is kicked out immediately');
r = await jar()('POST', '/api/auth/login', { email, password: 'password123' }); ok([401, 403].includes(r.status), 'suspended user cannot log in');
r = await a('PATCH', '/api/admin/users/' + row.id, { status: 'active' }); ok(r.status === 200 && r.data.status === 'active', 'admin reactivates user');
ok((await jar()('POST', '/api/auth/login', { email, password: 'password123' })).status === 200, 'reactivated user can log in');
r = await a('POST', '/api/admin/users', { email: `made${Date.now()}@example.com`, password: 'password123', role: 'admin' }); ok(r.status === 403, 'creating an admin via the API is refused');
const made = `made${Date.now()}@example.com`; r = await a('POST', '/api/admin/users', { email: made, password: 'password123' }); ok(r.status === 200 && r.data.email === made, 'admin creates a user');
r = await a('DELETE', '/api/admin/users/' + r.data.id); ok(r.status === 200, 'admin deletes a user');
r = await a('GET', '/api/admin/audit'); ok(r.data.audit.length >= 3, 'audit log records admin actions');
const me2 = (await a('GET', '/api/me')).data; r = await a('DELETE', '/api/admin/users/' + me2.id); ok(r.status === 400, "admin can't delete themselves");
console.log(bad ? `\n${bad} FAILED` : '\nall passed'); process.exit(bad ? 1 : 0);
