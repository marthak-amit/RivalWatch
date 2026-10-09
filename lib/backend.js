// Integration with the Python backend (backend/). Switched on with BACKEND_URL.
//
// The browser keeps talking to this server's /api/* contract (unchanged). Where the backend has an
// equivalent endpoint we call it; where it doesn't yet (competitor monitoring, reports, plan changes...)
// server.js keeps using the local implementation. The backend's bearer token never reaches page JS:
// it lives in the HttpOnly `rw_session` cookie.
import { PLANS } from './plans.js';

const TIMEOUT_MS = 20_000;
const STALE_SOCKET = new Set(['ECONNRESET', 'EPIPE', 'UND_ERR_SOCKET', 'UND_ERR_CLOSED']);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export class BackendError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

/** One human-readable message from FastAPI (`detail`) or crawl-API (`error.message`) error bodies. */
export function errorMessage(data, status) {
  const d = data?.detail;
  if (typeof d === 'string') return d;
  if (Array.isArray(d) && d.length) {
    const field = d[0].loc?.at(-1);
    if (field === 'email') return 'Enter a valid email address';
    if (field === 'password') return 'Password must be at least 8 characters';
    return d[0].msg || 'Invalid request';
  }
  return data?.error?.message || (typeof data?.error === 'string' ? data.error : '') || `Backend error (${status})`;
}

export class Backend {
  constructor(baseUrl) { this.base = String(baseUrl).replace(/\/+$/, ''); }

  /** Low-level call. Network failures become a 503 BackendError; HTTP errors are returned, not thrown.
   *  A reused keep-alive socket the backend already closed (it does this after an unhandled 500, or on restart) fails
   *  before anything is sent; for safe-to-repeat calls (`retry`, default GET/HEAD) we retry once on a fresh connection. */
  async raw(method, path, { token, apiKey, body, headers = {}, retry = method === 'GET' || method === 'HEAD' } = {}) {
    const h = { ...headers };
    if (token) h.authorization = 'Bearer ' + token;
    if (apiKey) h['x-api-key'] = apiKey;
    let payload;
    if (body !== undefined) {
      payload = typeof body === 'string' || Buffer.isBuffer(body) ? body : JSON.stringify(body);
      h['content-type'] ??= 'application/json';
    }
    let res;
    for (let attempt = 0; ; attempt++) {
      try { res = await fetch(this.base + path, { method, headers: h, body: payload, signal: AbortSignal.timeout(TIMEOUT_MS) }); break; }
      catch (e) {
        if (attempt === 0 && retry && STALE_SOCKET.has(e?.cause?.code)) continue; // stale pooled connection: try once more
        throw new BackendError(503, 'The backend is unavailable. Try again shortly.');
      }
    }
    return { status: res.status, buf: Buffer.from(await res.arrayBuffer()), type: res.headers.get('content-type') || '', headers: res.headers };
  }

  async json(method, path, opts) {
    const r = await this.raw(method, path, opts);
    let data = null;
    if (r.buf.length) { try { data = JSON.parse(r.buf.toString('utf8')); } catch { /* non-JSON body */ } }
    return { status: r.status, data };
  }

  /** Like json() but throws BackendError for any status >= 400. */
  async ok(method, path, opts) {
    const r = await this.json(method, path, opts);
    if (r.status >= 400) throw new BackendError(r.status === 422 ? 400 : r.status, errorMessage(r.data, r.status));
    return r.data;
  }

  // ---- auth ---------------------------------------------------------------------------------
  /** One login form for both roles: try the user login, then the admin login. */
  async login(email, password) {
    for (const [path, role] of [['/user/login', 'user'], ['/admin/login', 'admin']]) {
      const r = await this.json('POST', path, { body: { email, password }, retry: true }); // logging in twice is harmless
      if (r.status === 200) return { ...r.data, role: r.data.role || role };
      if (r.status !== 401) throw new BackendError(r.status === 422 ? 400 : r.status, errorMessage(r.data, r.status));
    }
    throw new BackendError(401, 'Incorrect email or password, or the account is disabled');
  }

  async signup(email, password) {
    const r = await this.json('POST', '/user/signup', { body: { email, password } });
    if (r.status === 201) return r.data;
    throw new BackendError(r.status === 409 ? 409 : r.status === 422 ? 400 : r.status,
      r.status === 409 ? 'An account with this email already exists' : errorMessage(r.data, r.status));
  }

  /** The signed-in account, or null when the token is missing, expired, revoked or the account is disabled. */
  async me(token) {
    const r = await this.json('GET', '/user/me', { token });
    if (r.status === 401) return null;
    if (r.status >= 400) throw new BackendError(r.status, errorMessage(r.data, r.status));
    return r.data;
  }

  async logout(token, role) { await this.raw('POST', role === 'admin' ? '/admin/logout' : '/user/logout', { token, retry: true }); }

  // ---- admin users --------------------------------------------------------------------------
  listUsers(token) { return this.ok('GET', '/admin/users', { token }); }
  setActive(token, id, isActive) { return this.ok('PATCH', `/admin/users/${encodeURIComponent(id)}`, { token, body: { is_active: !!isActive }, retry: true }); }
  deleteUser(token, id) { return this.ok('DELETE', `/admin/users/${encodeURIComponent(id)}`, { token }); }
}

// ---- identity -----------------------------------------------------------------------------------
const titleCase = (s) => s.replace(/[._-]+/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());

/** Backend account -> the user object the rest of server.js already works with. */
export function toUser(me) {
  return {
    id: String(me.id), email: me.email, role: me.role, status: 'active',
    name: me.name || titleCase(String(me.email).split('@')[0]),   // the backend doesn't store names yet
    plan: PLANS[me.plan] ? me.plan : 'starter', apiKey: me.api_key, credits: me.credits,
    createdAt: null, lastLoginAt: me.last_login_at ?? null,       // created_at isn't on /user/me
  };
}

/** Resolves the session cookie to a user via GET /user/me, cached briefly so polling doesn't hammer the backend. */
export class BackendIdentity {
  constructor(backend, ttlMs = 4000) { this.b = backend; this.ttl = ttlMs; this.cache = new Map(); }

  async resolve(token) {
    if (!token) return null;
    const hit = this.cache.get(token);
    if (hit && Date.now() - hit.at < this.ttl) return hit.user;
    const me = await this.b.me(token);
    if (!me) { this.cache.delete(token); return null; }
    const user = { ...toUser(me), token };
    this.cache.set(token, { user, at: Date.now() });
    if (this.cache.size > 500) this.cache.delete(this.cache.keys().next().value);
    return user;
  }
  invalidate(token) { this.cache.delete(token); }
  invalidateUser(id) { for (const [t, v] of this.cache) if (v.user.id === String(id)) this.cache.delete(t); }
}

/** Admin list row in the shape admin.html reads. Fields the backend doesn't have yet are null. */
export function toAdminRow(u, competitorCount = null) {
  return {
    id: String(u.id), email: u.email, role: u.role, status: u.is_active ? 'active' : 'suspended',
    name: titleCase(String(u.email).split('@')[0]), plan: null, credits: null, creditLimit: null,
    competitors: competitorCount, createdAt: u.created_at, lastLoginAt: u.last_login_at ?? null,
    activeNow: !!u.active_now, sessions: u.session_count ?? 0, totalSessionSec: u.total_session_seconds ?? 0,
  };
}

// ---- crawling through the backend's /v1 API ------------------------------------------------------
/** Same surface the Monitor uses from the local Crawler (startCrawl + waitForJob), backed by POST/GET /v1/web/crawl. */
export class RemoteCrawler {
  constructor(backend, getKey) { this.b = backend; this.getKey = getKey; }

  async startCrawl(url, opts = {}) {
    const r = await this.b.json('POST', '/v1/web/crawl', { apiKey: this.getKey(), body: {
      url, limit: opts.limit ?? 10, max_depth: opts.max_depth ?? 1, include_paths: opts.include_paths ?? [],
      exclude_paths: opts.exclude_paths ?? [], allow_external_links: !!opts.allow_external_links } });
    if (r.status !== 202 && r.status !== 200) throw new Error(errorMessage(r.data, r.status));
    return { job_id: r.data.data.job_id };
  }

  async waitForJob(id, timeoutMs = 120_000) {
    const t0 = Date.now();
    let delay = 300;
    for (;;) {
      const r = await this.b.json('GET', `/v1/web/crawl/${encodeURIComponent(id)}`, { apiKey: this.getKey() });
      if (r.status !== 200) throw new Error(errorMessage(r.data, r.status));
      const j = r.data.data;
      if (j.status === 'completed' || j.status === 'failed') return { status: j.status, error: j.error, result: j.result, progress: j.progress };
      if (Date.now() - t0 > timeoutMs) throw new Error('The crawl is taking too long. Is the backend crawl worker running?');
      await sleep(delay); delay = Math.min(Math.round(delay * 1.4), 2000);
    }
  }
}

/** Real sites go through the backend (credits, robots.txt, SSRF checks); this app's own demo pages stay local. */
export class RoutingCrawler {
  constructor(local, remote) { this.local = local; this.remote = remote; this.owner = new Map(); }

  async startCrawl(url, opts) {
    const target = this.local.allowLocal(new URL(url)) ? this.local : this.remote;
    const job = await target.startCrawl(url, opts);
    this.owner.set(job.job_id, target);
    if (this.owner.size > 200) this.owner.delete(this.owner.keys().next().value);
    return job;
  }
  waitForJob(id, timeoutMs) { return (this.owner.get(id) ?? this.remote).waitForJob(id, timeoutMs); }
  get credits() { return this.local.credits; }
  set credits(v) { this.local.credits = v; }
}

/** Shape check before credentials are forwarded. The backend rejects NUL bytes with a 500 (psycopg DataError), so keep
 *  control characters out, and mirror its length limits (email 254, password 128). Returns an error message or null. */
export function credentialProblem(email, password, { forSignup = false } = {}) {
  if (typeof email !== 'string' || typeof password !== 'string') return 'Enter your email and password';
  if (/[\u0000-\u001f\u007f]/.test(email) || /[\u0000-\u001f\u007f]/.test(password)) return 'Email or password contains invalid characters';
  if (email.length > 254 || password.length > 128) return forSignup ? 'Email or password is too long' : 'Incorrect email or password';
  return null;
}
