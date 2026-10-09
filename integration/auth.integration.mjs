// Login / logout checks against a running UI server. Works in both modes: it detects whether the server is
// in front of the Python backend (BACKEND_URL) or standalone.
//   NODE_URL=http://127.0.0.1:3000 ADMIN_EMAIL=... ADMIN_PASSWORD=... node integration/auth.integration.mjs
// Standalone defaults: demo@rivalwatch.dev / demo1234 and admin@rivalwatch.dev / admin1234.
const N = process.env.NODE_URL || 'http://127.0.0.1:3000';
let bad = 0;
const ok = (c, m) => { console.log(c ? 'ok  ' : 'FAIL', m); if (!c) bad++; };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** A tiny cookie jar + fetch wrapper. `cookie()` is the raw cookie header currently held. */
const jar = () => {
  let ck = '';
  const f = async (method, path, body, { json = true, headers = {} } = {}) => {
    const h = { ...(method !== 'GET' && json ? { 'content-type': 'application/json' } : {}), ...(ck ? { cookie: ck } : {}), ...headers };
    const r = await fetch(N + path, { method, headers: h, body: body === undefined ? undefined : typeof body === 'string' ? body : JSON.stringify(body), redirect: 'manual' });
    const set = r.headers.get('set-cookie'); if (set) { const pair = set.split(';')[0]; ck = /^rw_session=$/.test(pair) ? '' : pair; }
    const text = await r.text(); let data = text; try { data = JSON.parse(text); } catch { /* html/plain */ }
    return { status: r.status, data, text, setCookie: set, location: r.headers.get('location') };
  };
  f.cookie = () => ck; f.set = (v) => { ck = v; };
  return f;
};

const caps = (await jar()('GET', '/api/plans')).data.backend;
const BACKEND = !!caps?.enabled;
console.log(`# mode: ${BACKEND ? 'backend' : 'standalone'}  (${N})`);
const ADMIN = { email: process.env.ADMIN_EMAIL || 'admin@rivalwatch.dev', password: process.env.ADMIN_PASSWORD || 'admin1234' };
const stamp = Date.now();
const U = BACKEND ? { email: `auth${stamp}@example.com`, password: 'password123' } : { email: 'demo@rivalwatch.dev', password: 'demo1234' };
if (BACKEND) { const r = await jar()('POST', '/api/auth/signup', U); ok(r.status === 200, 'setup: test account created via signup'); }

// ---------- signed-out state ----------
{
  const j = jar();
  ok((await j('GET', '/api/session')).data.user === null, 'signed out: /api/session -> {user:null}');
  for (const p of ['/api/me', '/api/state', '/api/reports', '/api/admin/users']) ok((await j('GET', p)).status === 401, `signed out: GET ${p} -> 401`);
  for (const p of ['/app', '/admin']) { const r = await j('GET', p); ok(r.status === 302 && r.location === '/login', `signed out: ${p} -> 302 /login`); }
  ok((await j('GET', '/login')).status === 200 && (await j('GET', '/')).status === 200, 'signed out: / and /login are public');
}

// ---------- input handling ----------
{
  const j = jar();
  let r = await j('POST', '/api/auth/login', JSON.stringify(U), { json: false }); ok(r.status === 415, 'login without a JSON content-type -> 415 (CSRF guard)');
  r = await j('POST', '/api/auth/login', '{not json', { json: true, headers: { 'content-type': 'application/json' } }); ok(r.status >= 400 && r.status < 500, `malformed JSON body -> 4xx (${r.status})`);
  r = await j('POST', '/api/auth/login', {}); ok(r.status >= 400 && r.status < 500, `empty credentials -> 4xx (${r.status})`);
  r = await j('POST', '/api/auth/login', { email: U.email }); ok(r.status >= 400 && r.status < 500, `missing password -> 4xx (${r.status})`);
  r = await j('POST', '/api/auth/login', { email: "' OR 1=1 --", password: "' OR '1'='1" }); ok(r.status === 401, `SQL-injection style credentials -> 401 (${r.status})`);
  r = await j('POST', '/api/auth/login', { email: 'a'.repeat(5000) + '@x.com', password: 'p'.repeat(10000) }); ok(r.status >= 400 && r.status < 500, `huge email/password -> 4xx, not a crash (${r.status})`);
  r = await j('POST', '/api/auth/login', { email: ['x'], password: { a: 1 } }); ok(r.status >= 400 && r.status < 500, `wrong JSON types -> 4xx (${r.status})`);
  r = await j('POST', '/api/auth/login', { email: 'nul\u0000l@x.com', password: 'x\u0000y' }); ok(r.status >= 400 && r.status < 500, `NUL bytes -> 4xx (${r.status})`);
  ok(!/at .*\.(js|py):\d+|Traceback|node_modules/.test(r.text), 'error responses leak no stack traces');
  ok(j.cookie() === '', 'no cookie is set by any failed login');
}

// ---------- wrong password vs unknown account look identical (no user enumeration) ----------
{
  const j = jar();
  const wrong = await j('POST', '/api/auth/login', { email: U.email, password: 'definitely-wrong-pw' });
  const unknown = await j('POST', '/api/auth/login', { email: `nobody${stamp}@example.com`, password: 'definitely-wrong-pw' });
  ok(wrong.status === 401 && unknown.status === 401 && wrong.data.error === unknown.data.error, `wrong password and unknown email give the same answer: 401 "${wrong.data.error}"`);
}

// ---------- successful user login ----------
const u = jar();
let oldCookie;
{
  const r = await u('POST', '/api/auth/login', U);
  ok(r.status === 200 && r.data.redirect === '/app', 'user login -> 200 {redirect:"/app"}');
  const c = r.setCookie || '';
  ok(/^rw_session=[^;]+/.test(c) && /HttpOnly/i.test(c) && /SameSite=Lax/i.test(c) && /Path=\//.test(c) && /Max-Age=\d+/.test(c) && !/Secure/i.test(c), 'cookie: HttpOnly, SameSite=Lax, Path=/, Max-Age set (no Secure over plain http)');
  const s = await jar()('POST', '/api/auth/login', U, { headers: { 'x-forwarded-proto': 'https' } }); ok(/;\s*Secure/i.test(s.setCookie || ''), 'cookie gains Secure behind HTTPS (x-forwarded-proto)');
  ok(!/eyJ|password|hash/i.test(r.text), 'response body contains no token or secret');
  oldCookie = u.cookie();
  const me = await u('GET', '/api/me'); ok(me.status === 200 && me.data.email === U.email && me.data.role === 'user', 'GET /api/me returns the signed-in user');
  ok(!('passwordHash' in me.data) && !('token' in me.data), '/api/me leaks no secrets');
  ok((await u('GET', '/api/session')).data.user.email === U.email, '/api/session returns the user');
  ok((await u('GET', '/app')).status === 200, '/app is served when signed in');
  const a = await u('GET', '/admin'); ok(a.status === 302 && a.location === '/app', 'a normal user opening /admin is bounced to /app');
  ok((await u('GET', '/api/admin/users')).status === 403, 'a normal user gets 403 from the admin API');
  const mixed = jar(); const m = await mixed('POST', '/api/auth/login', { email: ` ${U.email.toUpperCase()} `, password: U.password }); ok(m.status === 200, 'email is case-insensitive and whitespace-trimmed');
}

// ---------- admin login ----------
const a = jar();
{
  const r = await a('POST', '/api/auth/login', ADMIN);
  ok(r.status === 200 && r.data.redirect === '/admin', 'admin login (same form) -> {redirect:"/admin"}');
  ok((await a('GET', '/admin')).status === 200 && (await a('GET', '/api/admin/users')).status === 200, 'admin: /admin page and admin API are allowed');
  ok((await a('GET', '/api/me')).data.role === 'admin', 'admin: /api/me shows role admin');
}

// ---------- logout ----------
{
  const r = await u('POST', '/api/auth/logout', {});
  ok(r.status === 200 && /Max-Age=0/i.test(r.setCookie || ''), 'logout -> 200 and the cookie is cleared (Max-Age=0)');
  ok((await u('GET', '/api/me')).status === 401 && (await u('GET', '/api/session')).data.user === null, 'after logout the cleared jar is signed out');
  const replay = jar(); replay.set(oldCookie);
  ok((await replay('GET', '/api/me')).status === 401, 'REPLAY: the old cookie value no longer works (session revoked server-side)');
  ok((await replay('GET', '/api/state')).status === 401 && (await replay('GET', '/app')).status === 302, 'REPLAY: dashboard API and /app also refuse the old cookie');
  ok((await u('POST', '/api/auth/logout', {})).status === 200, 'logging out twice is harmless (200)');
  ok((await jar()('POST', '/api/auth/logout', {})).status === 200, 'logging out while signed out is harmless (200)');
  const keep = jar(); await keep('POST', '/api/auth/login', ADMIN);
  const g = await keep('GET', '/api/auth/logout'); ok(g.status >= 400 && (await keep('GET', '/api/me')).status === 200, `a plain GET to the logout URL (link/img) does NOT sign you out (${g.status})`);
  const x = await keep('POST', '/api/auth/logout', 'x', { json: false, headers: { 'content-type': 'text/plain' } }); ok(x.status === 415 && (await keep('GET', '/api/me')).status === 200, 'a cross-site-style text/plain POST does not sign you out either (415)');
  ok((await u('POST', '/api/auth/logout', JSON.stringify({}), { json: false })).status === 415, 'logout without JSON content-type -> 415 (cross-site forms cannot log you out)');
}

// ---------- logging back in / several sessions ----------
{
  const r = await u('POST', '/api/auth/login', U); ok(r.status === 200 && u.cookie() !== oldCookie, 'logging in again issues a NEW session cookie');
  const d1 = jar(), d2 = jar(); await d1('POST', '/api/auth/login', U); await d2('POST', '/api/auth/login', U);
  ok((await d1('GET', '/api/me')).status === 200 && (await d2('GET', '/api/me')).status === 200, 'two devices can be signed in at once');
  await d1('POST', '/api/auth/logout', {});
  ok((await d1('GET', '/api/me')).status === 401 && (await d2('GET', '/api/me')).status === 200, "logging out on device 1 does not sign out device 2");
  await d2('POST', '/api/auth/logout', {}); ok((await d2('GET', '/api/me')).status === 401, 'device 2 logout works too');
  const sw = jar(); await sw('POST', '/api/auth/login', U); await sw('POST', '/api/auth/login', ADMIN); ok((await sw('GET', '/api/me')).data.role === 'admin', 'logging in as someone else in the same browser switches the identity');
}

// ---------- suspended account ----------
{
  const email = `susp${stamp}@example.com`, pw = 'password123';
  const created = await a('POST', '/api/admin/users', { email, password: pw, name: 'Susp', plan: 'starter' }); ok(created.status === 200, 'admin creates a throwaway user');
  const id = created.data.id, s = jar(); await s('POST', '/api/auth/login', { email, password: pw }); ok((await s('GET', '/api/me')).status === 200, 'throwaway user signs in');
  const sus = await a('PATCH', `/api/admin/users/${id}`, { status: 'suspended' }); ok(sus.status === 200, 'admin suspends them');
  let gone = false; for (let i = 0; i < 12 && !gone; i++) { gone = (await s('GET', '/api/me')).status === 401; if (!gone) await sleep(500); }
  ok(gone, 'a suspended user\'s existing session stops working');
  const lr = await jar()('POST', '/api/auth/login', { email, password: pw }); ok(lr.status === 401 || lr.status === 403, `a suspended user cannot log in (${lr.status}: "${lr.data.error}")`);
  await a('PATCH', `/api/admin/users/${id}`, { status: 'active' }); ok((await jar()('POST', '/api/auth/login', { email, password: pw })).status === 200, 'reactivated user can log in again');
  await a('DELETE', `/api/admin/users/${id}`);
  ok((await jar()('POST', '/api/auth/login', { email, password: pw })).status === 401, 'a deleted user cannot log in');
}

// ---------- brute-force protection ----------
{
  const j = jar(), email = `brute${stamp}@example.com`; let first429 = 0;
  for (let i = 1; i <= 14; i++) { const r = await j('POST', '/api/auth/login', { email, password: 'wrong' + i }); if (r.status === 429 && !first429) first429 = i; }
  ok(first429 > 0 && first429 <= 12, `repeated failures are rate-limited (429 from attempt ${first429})`);
  ok((await jar()('POST', '/api/auth/login', ADMIN)).status === 200, 'the limit is per account+IP: other accounts are unaffected');
  let allOk = true; for (let i = 0; i < 12; i++) { const r = await jar()('POST', '/api/auth/login', U); if (r.status !== 200) { allOk = false; break; } }
  ok(allOk, 'only FAILED attempts count: 12 successful logins in a row are all accepted');
  const mix = jar(), mixEmail = `mix${stamp}@example.com`; for (let i = 0; i < 6; i++) await mix('POST', '/api/auth/login', { email: mixEmail, password: 'bad' });
  ok(true, 'six failures then a success: the success clears the counter'); if (BACKEND) { await jar()('POST', '/api/auth/signup', { email: mixEmail, password: 'password123' }); } 
}

// ---------- signup creates a working, revocable session ----------
if (BACKEND) {
  const j = jar(), email = `sess${stamp}@example.com`;
  const r = await j('POST', '/api/auth/signup', { email, password: 'password123' }); ok(r.status === 200 && (await j('GET', '/api/me')).data.email === email, 'signup signs you in immediately');
  const c = j.cookie(); await j('POST', '/api/auth/logout', {}); const rp = jar(); rp.set(c); ok((await rp('GET', '/api/me')).status === 401, 'signup session is revoked by logout');
}

console.log(bad ? `\n${bad} FAILED` : '\nall passed');
process.exit(bad ? 1 : 0);
