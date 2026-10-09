// Scheduled-crawl pipeline: crawl -> snapshot -> diff -> digest.
import { extract } from './extract.js';
import { diff, describe } from './diff.js';
import { makeDigest } from './digest.js';

export class Monitor {
  constructor(crawler, competitors) {
    this.crawler = crawler;
    this.competitors = competitors; // [{id, name, url}]
    this.state = { snapshots: {}, changes: [], digests: [], runs: 0, lastRun: null };
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
    try {
      for (const c of this.competitors.filter((x) => !onlyIds || onlyIds.includes(x.id))) {
        try {
          const snap = await this.#snapshot(c);
          const hist = (this.state.snapshots[c.id] ??= []);
          const prev = hist.at(-1);
          hist.push(snap);
          if (hist.length > 20) hist.shift();
          if (prev) for (const ch of diff(prev, snap)) newChanges.push({ ...ch, competitor: c.name, competitorId: c.id });
          c.error = null;
        } catch (e) { c.error = e.message; }
      }
      const ts = new Date().toISOString();
      newChanges.forEach((ch, i) => { ch.ts = ts; ch.id = `${Date.now()}-${i}`; ch.label = describe(ch); });
      this.state.changes = [...newChanges.reverse(), ...this.state.changes].slice(0, 100);
      if (newChanges.length) {
        const d = await makeDigest(newChanges);
        this.state.digests.unshift({ ts, changeCount: newChanges.length, ...d });
        this.state.digests = this.state.digests.slice(0, 10);
      }
      this.state.runs++; this.state.lastRun = ts;
      return { skipped: false, changes: newChanges };
    } finally { this.running = false; }
  }

  view() {
    return {
      competitors: this.competitors.map((c) => {
        const s = this.state.snapshots[c.id]?.at(-1);
        return { id: c.id, name: c.name, url: c.url, site: c.site ?? null, error: c.error ?? null, snapshot: s ?? null, snapshots: this.state.snapshots[c.id]?.length ?? 0 };
      }),
      changes: this.state.changes, digest: this.state.digests[0] ?? null,
      runs: this.state.runs, lastRun: this.state.lastRun, running: this.running,
    };
  }

  add(c) { this.competitors.push(c); }
  remove(id) { this.competitors = this.competitors.filter((c) => c.id !== id); delete this.state.snapshots[id]; this.state.changes = this.state.changes.filter((ch) => ch.competitorId !== id); }
  reset() { this.state = { snapshots: {}, changes: [], digests: [], runs: 0, lastRun: null }; for (const c of this.competitors) c.error = null; }
}
