// Scheduled-crawl pipeline: crawl -> snapshot -> diff -> digest.
import { extract } from './extract.js';
import { diff, describe } from './diff.js';
import { makeDigest } from './digest.js';

export class Monitor {
  constructor(crawler, competitors) {
    this.crawler = crawler;
    this.competitors = competitors; // [{id, name, url}]
    this.state = { snapshots: {}, changes: [], digests: [], crawlLog: [], runs: 0, lastRun: null };
    this.running = false;
  }

  async #snapshot(c) {
    const job = this.crawler.startCrawl(c.url, { limit: 8, max_depth: 1 });
    const done = await this.crawler.waitForJob(job.job_id);
    const pages = (done.result?.pages ?? []).filter((p) => !p.error);
    if (!pages.length) throw new Error(done.error || done.result?.pages?.find((p) => p.error)?.error || 'no pages fetched');
    const merged = { products: {}, promotions: [], pages: [] };
    const pageSet = new Set();
    const root = new URL(c.url).pathname.replace(/\/$/, '');
    const rel = (x) => x.replace(/\/$/, '').slice(x.startsWith(root) ? root.length : 0) || '/';
    for (const p of pages) {
      const e = extract(p.markdown, c.url);
      Object.assign(merged.products, e.products);
      for (const x of e.promotions) if (!merged.promotions.includes(x)) merged.promotions.push(x);
      e.pages.forEach((x) => pageSet.add(x));
      pageSet.add(rel(new URL(p.url).pathname));
    }
    merged.pages = [...pageSet].sort();
    return { ts: new Date().toISOString(), pagesCrawled: pages.length, ...merged };
  }

  /** Run one crawl over all competitors. Returns the new changes. */
  async run(onlyIds) {
    while (this.running) await this.current.catch(() => {}); // queue behind an in-flight crawl
    this.running = true;
    this.current = this.#run(onlyIds);
    return this.current;
  }

  async #run(onlyIds) {
    const newChanges = [];
    let pages = 0;
    try {
      for (const c of this.competitors.filter((x) => !onlyIds || onlyIds.includes(x.id))) {
        try {
          const snap = await this.#snapshot(c);
          const hist = (this.state.snapshots[c.id] ??= []);
          const prev = hist.at(-1);
          hist.push(snap); pages += snap.pagesCrawled;
          if (hist.length > 20) hist.shift();
          if (prev) for (const ch of diff(prev, snap)) newChanges.push({ ...ch, competitor: c.name, competitorId: c.id });
          c.error = null;
        } catch (e) { c.error = e.message; }
      }
      const ts = new Date().toISOString();
      newChanges.forEach((ch, i) => { ch.ts = ts; ch.id = `${Date.now()}-${i}`; ch.label = describe(ch); });
      this.state.changes = [...newChanges.reverse(), ...this.state.changes].slice(0, 1000);
      if (newChanges.length) {
        const d = await makeDigest(newChanges);
        this.state.digests.unshift({ ts, changeCount: newChanges.length, ...d });
        this.state.digests = this.state.digests.slice(0, 10);
      }
      this.state.crawlLog.push({ ts, changes: newChanges.length, pages });
      this.state.crawlLog = this.state.crawlLog.slice(-1000);
      this.state.runs++; this.state.lastRun = ts;
      return { skipped: false, changes: newChanges };
    } finally { this.running = false; }
  }

  /** Pre-load ~30 days of plausible history so weekly/monthly reports have something to show. Flagged `sample`. */
  seedHistory(now = Date.now()) {
    const T = (daysAgo, hour) => new Date(now - daysAgo * 864e5 - hour * 36e5).toISOString();
    const rows = [
      [27, 'acme', { signal: 'product', type: 'price_change', name: 'Pro', from: '$69/mo', to: '$79/mo', pct: 14.5 }],
      [25, 'globex', { signal: 'page', type: 'new_promo', text: 'Free setup for annual plans' }],
      [22, 'initech', { signal: 'page', type: 'new_page', path: '/integrations' }],
      [20, 'acme', { signal: 'product', type: 'product_added', name: 'Business', price: '$199/mo' }],
      [17, 'globex', { signal: 'product', type: 'price_change', name: 'Team', from: '$49/mo', to: '$59/mo', pct: 20.4 }],
      [13, 'initech', { signal: 'product', type: 'price_change', name: 'Scale', from: '$135/mo', to: '$120/mo', pct: -11.1 }],
      [11, 'acme', { signal: 'page', type: 'new_promo', text: 'Black week: 25% off Pro for 3 months' }],
      [9, 'globex', { signal: 'page', type: 'new_page', path: '/case-studies' }],
      [8, 'initech', { signal: 'page', type: 'promo_ended', text: 'Spring sale: 20% off annual plans' }],
      [6, 'acme', { signal: 'product', type: 'price_change', name: 'Starter', from: '$25/mo', to: '$29/mo', pct: 16 }],
      [5, 'initech', { signal: 'page', type: 'new_page', path: '/security' }],
      [3, 'globex', { signal: 'product', type: 'product_added', name: 'Solo', price: '$19/mo' }],
      [2, 'initech', { signal: 'product', type: 'price_change', name: 'Plus', from: '$49/mo', to: '$45/mo', pct: -8.2 }],
      [1, 'acme', { signal: 'page', type: 'new_page', path: '/changelog' }],
    ];
    const seeded = rows.flatMap(([d, site, ch], i) => {
      const c = this.competitors.find((x) => x.id === site);
      return c ? [{ ...ch, competitor: c.name, competitorId: c.id, ts: T(d, 3 + (i % 5)), id: 's' + i, label: describe(ch), sample: true }] : [];
    });
    this.state.changes = [...this.state.changes, ...seeded].sort((a, b) => b.ts.localeCompare(a.ts));
    const days = new Set(seeded.map((c) => c.ts.slice(0, 10)));
    const log = Array.from({ length: 30 }, (_, i) => {
      const ts = T(29 - i, 18);
      return { ts, pages: 10, changes: seeded.filter((c) => c.ts.slice(0, 10) === ts.slice(0, 10)).length, sample: true };
    }).filter((r) => r.ts < new Date(now - 36e5).toISOString());
    this.state.crawlLog = [...log, ...this.state.crawlLog];
    return days.size;
  }

  view() {
    return {
      competitors: this.competitors.map((c) => {
        const s = this.state.snapshots[c.id]?.at(-1);
        return { id: c.id, name: c.name, url: c.url, site: c.site ?? null, error: c.error ?? null, snapshot: s ?? null, snapshots: this.state.snapshots[c.id]?.length ?? 0 };
      }),
      changes: this.state.changes, digest: this.state.digests[0] ?? null, digests: this.state.digests,
      runs: this.state.runs, lastRun: this.state.lastRun, running: this.running,
    };
  }

  add(c) { this.competitors.push(c); }
  remove(id) { this.competitors = this.competitors.filter((c) => c.id !== id); delete this.state.snapshots[id]; this.state.changes = this.state.changes.filter((ch) => ch.competitorId !== id); }
  reset() { this.state = { snapshots: {}, changes: [], digests: [], crawlLog: [], runs: 0, lastRun: null }; for (const c of this.competitors) c.error = null; }
}
