// Native mode: the backend serves the UI's own /api/* contract (same paths and shapes as the standalone server), so this
// server only hands out the static pages and forwards /api/* and /v1/* unchanged, cookies included.
// Used automatically when GET <BACKEND_URL>/api/plans answers with `backend.enabled` (override with BACKEND_MODE=native|bff).
import { BackendError } from './backend.js';

const MAX_BODY = 1e6; // capped at 1 MB so the proxy can't be used to exhaust memory

/** Request headers worth forwarding (never host/connection/content-length: fetch sets those). */
const FORWARD = ['content-type', 'cookie', 'x-api-key', 'authorization', 'accept', 'accept-language', 'user-agent'];

export function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = []; let n = 0;
    req.on('data', (d) => { n += d.length; if (n > MAX_BODY) { req.destroy(); reject(new BackendError(413, 'Request body too large')); } else chunks.push(d); });
    req.on('end', () => resolve(Buffer.concat(chunks)));
    req.on('error', reject);
  });
}

/** Detects once whether the backend speaks /api/*: a positive answer is kept, a negative one is re-checked after 10 s. */
export class ModeProbe {
  constructor(backend, mode = 'auto', retryMs = 10_000, downRetryMs = 1_000) {
    this.b = backend; this.mode = mode; this.retryMs = retryMs; this.downRetryMs = downRetryMs; this.native = null; this.at = 0; this.wait = 0; this.answered = false;
  }

  async isNative() {
    if (!this.b || this.mode === 'bff') return false;
    if (this.mode === 'native') return true;
    if (this.native === true) return true;
    if (this.native === false && Date.now() - this.at < this.wait) return false;
    this.at = Date.now();
    try {
      const r = await this.b.json('GET', '/api/plans');
      this.native = r.status === 200 && r.data?.backend?.enabled === true;
      this.answered = true;
      this.wait = this.retryMs; // answered, but without /api/*: an older backend, look again rarely
    } catch { this.native = false; this.wait = this.downRetryMs; } // unreachable: look again soon (requests report the outage themselves)
    return this.native;
  }
}

/** Forwards one request to the backend and streams its answer back (status, content type, Set-Cookie, Retry-After). */
export async function forward(backend, req, res, url, baseHeaders) {
  const headers = {};
  for (const h of FORWARD) if (req.headers[h]) headers[h] = req.headers[h];
  const ip = req.socket.remoteAddress;
  headers['x-forwarded-for'] = [req.headers['x-forwarded-for'], ip].filter(Boolean).join(', '); // the backend's login limiter keys on this
  headers['x-forwarded-proto'] = req.headers['x-forwarded-proto'] || 'http';
  const safe = req.method === 'GET' || req.method === 'HEAD';
  // Safe calls are retried on a fresh connection if the pooled one went stale. Mutations are never repeated, so they
  // use a connection of their own: nothing can reuse a socket the backend closed after an unhandled error.
  if (!safe) headers.connection = 'close';
  const body = safe ? undefined : await readBody(req);
  const r = await backend.raw(req.method, url.pathname + url.search, { body: body?.length ? body : undefined, headers });
  const out = { ...baseHeaders, 'content-type': r.type || 'application/json', 'cache-control': 'no-store' };
  const cookies = r.headers?.getSetCookie?.() ?? [];
  if (cookies.length) out['set-cookie'] = cookies;
  const retry = r.headers?.get('retry-after'); if (retry) out['retry-after'] = retry;
  res.writeHead(r.status, out);
  res.end(r.buf);
}
