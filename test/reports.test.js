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
  assert.equal(month.biggestMoves[0].name, 'Team'); // +20.4% is the largest move
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
