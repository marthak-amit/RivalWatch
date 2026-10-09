// One isolated workspace per user: own crawler (credits + jobs), own monitor
// (snapshots, changes, digests) and own copy of the editable demo test sites.
import { Crawler } from './crawler.js';
import { Monitor } from './monitor.js';
import { PLANS } from './plans.js';
import * as testsite from './testsite.js';

export class Workspaces {
  constructor(port) { this.port = port; this.map = new Map(); }

  get(user) {
    let ws = this.map.get(user.id);
    if (ws) return ws;
    const port = this.port;
    const crawler = new Crawler({
      credits: PLANS[user.plan].credits,
      // Only this app's own /test/ pages may bypass the SSRF guard.
      allowLocal: (u) => ['localhost', '127.0.0.1'].includes(u.hostname) && +u.port === port && u.pathname.startsWith('/test/'),
    });
    const sites = testsite.seed();
    const competitors = Object.entries(sites).map(([site, s]) => ({
      id: site, site, name: s.name, url: `http://localhost:${port}/test/${user.id}/${site}/` }));
    const monitor = new Monitor(crawler, competitors);
    ws = { userId: user.id, crawler, monitor, sites };
    this.map.set(user.id, ws);
    monitor.run().catch((e) => console.error('baseline crawl failed', e)); // baseline snapshot
    return ws;
  }
  peek(userId) { return this.map.get(userId); }
  drop(userId) { this.map.delete(userId); }
  all() { return [...this.map.values()]; }
}
