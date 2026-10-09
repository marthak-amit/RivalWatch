import test from 'node:test';
import assert from 'node:assert/strict';
import { buildReport } from '../lib/reports.js';
import { Monitor } from '../lib/monitor.js';

const comps = [{ id: 'acme', name: 'Acme' }, { id: 'globex', name: 'Globex' }, { id: 'initech', name: 'Initech' }];

test('weekly vs monthly report windows and delta', () => {
  const m = new Monitor(null, comps);
  m.seedHistory();
  const week = buildReport(m.state, 'week'), month = buildReport(m.state, 'month');
  assert.equal(week.daily.length, 7); assert.equal(month.daily.length, 30);
  assert.equal(week.total, 5); assert.equal(week.prevTotal, 4); assert.equal(week.deltaPct, 25);
  assert.equal(month.total, 14);
  assert.equal(week.daily.reduce((s, d) => s + d.product + d.page, 0), week.total); // chart sums to headline
  assert.equal(month.biggestMoves[0].name, 'Insights Team'); // +20.4% is the largest move
  assert.ok(week.hasSample);
});

test('empty history yields a friendly report', () => {
  const r = buildReport({ changes: [], crawlLog: [] }, 'week');
  assert.equal(r.total, 0); assert.equal(r.deltaPct, null); assert.match(r.highlights[0], /No competitor changes/);
});

test('report can be scoped to one competitor', () => {
  const m = new Monitor(null, comps);
  m.seedHistory();
  const all = buildReport(m.state, 'month');
  const one = buildReport(m.state, 'month', undefined, 'acme');
  assert.ok(one.total > 0 && one.total < all.total);
  assert.equal(one.total, all.byCompetitor.find((c) => c.id === 'acme').total);
  assert.deepEqual(one.byCompetitor.map((c) => c.id), ['acme']);
  assert.equal(buildReport(m.state, 'week', undefined, 'nope').total, 0);
});

test('disabled competitors are skipped by scheduled crawls but can be re-checked on enable', async () => {
  const crawled = [];
  const crawler = {
    startCrawl: (url) => { crawled.push(url); return { job_id: url }; },
    waitForJob: async (id) => ({ status: 'completed', result: { pages: [{ url: id, title: 'Home', markdown: '# Hi\n\n- **Plan** — $10/mo', links: [] }] } }),
  };
  const m = new Monitor(crawler, [{ id: 'a', name: 'A', url: 'http://a.test/' }, { id: 'b', name: 'B', url: 'http://b.test/' }]);
  await m.run(); assert.deepEqual(crawled, ['http://a.test/', 'http://b.test/']);
  assert.equal(m.setEnabled('b', false), true); crawled.length = 0;
  await m.run(); assert.deepEqual(crawled, ['http://a.test/']);              // b skipped
  assert.equal(m.view().competitors.find((c) => c.id === 'b').enabled, false);
  m.setEnabled('b', true); crawled.length = 0;
  await m.run(['b']); assert.deepEqual(crawled, ['http://b.test/']);          // explicit re-check
  assert.equal(m.setEnabled('nope', true), false);
});
