import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

const hashPw = (pw) => { const salt = crypto.randomBytes(16); return salt.toString('hex') + ':' + crypto.scryptSync(pw, salt, 32).toString('hex'); };
const checkPw = (pw, stored) => {
  const [s, h] = stored.split(':');
  const got = crypto.scryptSync(pw, Buffer.from(s, 'hex'), 32);
  return crypto.timingSafeEqual(got, Buffer.from(h, 'hex'));
};
const DUMMY = hashPw('dummy-password'); // equalise timing for unknown emails
const newKey = () => 'rw_' + crypto.randomBytes(16).toString('hex');
export const normEmail = (e) => String(e || '').trim().toLowerCase();
export const validEmail = (e) => /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(e) && e.length <= 120;

export class UserStore {
  constructor(file) {
    this.file = file; this.users = new Map();
    try { for (const u of JSON.parse(fs.readFileSync(file, 'utf8'))) this.users.set(u.id, u); } catch { /* first run */ }
  }
  save() {
    try { fs.mkdirSync(path.dirname(this.file), { recursive: true }); fs.writeFileSync(this.file, JSON.stringify([...this.users.values()], null, 1), { mode: 0o600 }); } catch { /* best effort */ }
  }
  list() { return [...this.users.values()]; }
  byId(id) { return this.users.get(id); }
  byEmail(e) { e = normEmail(e); return this.list().find((u) => u.email === e); }
  byApiKey(k) { return typeof k === 'string' && k ? this.list().find((u) => u.apiKey === k) : undefined; }

  create({ email, name, password, plan = 'starter', role = 'user', status = 'active', createdAt, lastLoginAt = null }) {
    email = normEmail(email);
    if (!validEmail(email)) throw new Error('Enter a valid email address');
    if (this.byEmail(email)) throw new Error('An account with this email already exists');
    name = String(name || '').trim().slice(0, 60) || email.split('@')[0];
    if (String(password || '').length < 8) throw new Error('Password must be at least 8 characters');
    const u = { id: crypto.randomBytes(6).toString('hex'), email, name, role, plan, status, apiKey: newKey(),
      passwordHash: hashPw(password), createdAt: createdAt || new Date().toISOString(), lastLoginAt };
    this.users.set(u.id, u); this.save();
    return u;
  }
  authenticate(email, password) {
    const u = this.byEmail(email);
    const ok = checkPw(String(password || ''), u ? u.passwordHash : DUMMY);
    return u && ok ? u : null;
  }
  update(id, patch) { const u = this.users.get(id); Object.assign(u, patch); this.save(); return u; }
  setPassword(id, pw) {
    if (String(pw || '').length < 8) throw new Error('Password must be at least 8 characters');
    this.update(id, { passwordHash: hashPw(pw) });
  }
  rotateKey(id) { return this.update(id, { apiKey: newKey() }).apiKey; }
  remove(id) { this.users.delete(id); this.save(); }
}

export const publicUser = (u) => ({ id: u.id, email: u.email, name: u.name, role: u.role, plan: u.plan, status: u.status, createdAt: u.createdAt, lastLoginAt: u.lastLoginAt });

export class Sessions {
  constructor(ttlMs = 24 * 3600 * 1000) { this.ttl = ttlMs; this.map = new Map(); }
  create(userId) { const t = crypto.randomBytes(24).toString('hex'); this.map.set(t, { userId, exp: Date.now() + this.ttl }); return t; }
  get(t) { const s = this.map.get(t); if (!s) return null; if (s.exp < Date.now()) { this.map.delete(t); return null; } return s.userId; }
  destroy(t) { this.map.delete(t); }
  destroyForUser(id) { for (const [t, s] of this.map) if (s.userId === id) this.map.delete(t); }
}

// Naive in-memory limiter for login/signup attempts.
export class Limiter {
  constructor(max = 10, windowMs = 5 * 60_000) { this.max = max; this.win = windowMs; this.hits = new Map(); }
  hit(key) {
    const now = Date.now();
    const arr = (this.hits.get(key) || []).filter((t) => now - t < this.win);
    arr.push(now); this.hits.set(key, arr);
    return arr.length <= this.max;
  }
  /** Forget a key (called after a successful login so only failed attempts add up). */
  reset(key) { this.hits.delete(key); }
}
