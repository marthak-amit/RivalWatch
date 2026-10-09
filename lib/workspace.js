// One isolated workspace per user: own crawler (credits + jobs), own monitor
// (snapshots, changes, digests) and own copy of the editable demo test sites.
import { Crawler } from './crawler.js';
import { Monitor } from './monitor.js';
import { PLANS } from './plans.js';
import * as testsite from './testsite.js';

export class Workspaces {
  /** @param {{makeCrawler?: (local: Crawler, ws: object) => object, demo?: boolean}} opts
   *  makeCrawler wraps the local crawler (e.g. to send real sites through the backend); demo=false starts workspaces empty. */
  constructor(port, { makeCrawler, demo = true } = {}) { this.port = port; this.makeCrawler = makeCrawler; this.demo = demo; this.map = new Map(); }

  /** The three demo competitors (editable test sites) for a user's workspace. */
  demoCompetitors(user, sites) {
    return Object.entries(sites).map(([site, s]) => ({ id: site, site, name: s.name, url: `http://localhost:${this.port}/test/${user.id}/${site}/` }));
  }

  get(user) {
    let ws = this.map.get(user.id);
    if (ws) return ws;
    const port = this.port;
    const local = new Crawler({
      credits: PLANS[user.plan].credits,
      // Only this app's own /test/ pages may bypass the SSRF guard.
      allowLocal: (u) => ['localhost', '127.0.0.1'].includes(u.hostname) && +u.port === port && u.pathname.startsWith('/test/'),
    });
    const sites = testsite.seed();
    const competitors = this.demo ? this.demoCompetitors(user, sites) : [];
    ws = { userId: user.id, user, crawler: local, monitor: null, sites };
    ws.crawler = this.makeCrawler ? this.makeCrawler(local, ws) : local;
    ws.monitor = new Monitor(ws.crawler, competitors);
    const monitor = ws.monitor;
    this.map.set(user.id, ws);
    monitor.seedHistory(); // demo history for reports
    monitor.run().catch((e) => console.error('baseline crawl failed', e)); // baseline snapshot
    return ws;
  }
  peek(userId) { return this.map.get(userId); }
  drop(userId) { this.map.delete(userId); }
  all() { return [...this.map.values()]; }
}
