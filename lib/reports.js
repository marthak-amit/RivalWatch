// Weekly / monthly report over a workspace's change history and crawl log.
// Days are UTC calendar days so charts, totals and the previous-period delta always agree.
const dayKeys = (days, endMs, offsetDays = 0) =>
  Array.from({ length: days }, (_, i) => new Date(endMs - (days - 1 - i + offsetDays) * 864e5).toISOString().slice(0, 10));

export function buildReport({ changes, crawlLog }, period, now = Date.now()) {
  const days = period === 'month' ? 30 : 7;
  const keys = dayKeys(days, now);
  const prevKeys = new Set(dayKeys(days, now, days));
  const cur = new Set(keys);
  const inCur = changes.filter((c) => cur.has(c.ts.slice(0, 10)));
  const prevTotal = changes.filter((c) => prevKeys.has(c.ts.slice(0, 10))).length;

  const daily = keys.map((date) => ({ date, product: 0, page: 0 }));
  const byDate = Object.fromEntries(daily.map((d) => [d.date, d]));
  const byType = {};
  const comps = new Map();
  for (const c of inCur) {
    byDate[c.ts.slice(0, 10)][c.signal]++;
    byType[c.type] = (byType[c.type] || 0) + 1;
    const r = comps.get(c.competitorId) ?? { id: c.competitorId, name: c.competitor, total: 0, product: 0, page: 0, priceMoves: 0, promos: 0, newPages: 0 };
    r.total++; r[c.signal]++;
    if (c.type === 'price_change') r.priceMoves++;
    if (c.type === 'new_promo') r.promos++;
    if (c.type === 'new_page') r.newPages++;
    comps.set(c.competitorId, r);
  }
  const byCompetitor = [...comps.values()].sort((a, b) => b.total - a.total);
  const moves = inCur.filter((c) => c.type === 'price_change' && c.pct != null)
    .sort((a, b) => Math.abs(b.pct) - Math.abs(a.pct)).slice(0, 5)
    .map((c) => ({ competitor: c.competitor, name: c.name, from: c.from, to: c.to, pct: c.pct, ts: c.ts }));
  const runs = crawlLog.filter((r) => cur.has(r.ts.slice(0, 10)));

  const highlights = [];
  if (moves[0]) highlights.push(`Biggest price move: ${moves[0].competitor} ${moves[0].name} ${moves[0].from} → ${moves[0].to} (${moves[0].pct > 0 ? '+' : ''}${moves[0].pct}%).`);
  if (byCompetitor[0]) highlights.push(`Most active competitor: ${byCompetitor[0].name} with ${byCompetitor[0].total} change${byCompetitor[0].total === 1 ? '' : 's'}.`);
  if (byType.new_promo) highlights.push(`${byType.new_promo} new promotion${byType.new_promo === 1 ? '' : 's'} launched.`);
  if (byType.new_page) highlights.push(`${byType.new_page} new page${byType.new_page === 1 ? '' : 's'} published.`);
  if (!inCur.length) highlights.push('No competitor changes detected in this period.');

  return {
    period, days, from: keys[0], to: keys.at(-1), total: inCur.length, prevTotal,
    deltaPct: prevTotal ? Math.round(((inCur.length - prevTotal) / prevTotal) * 100) : null,
    bySignal: { product: inCur.filter((c) => c.signal === 'product').length, page: inCur.filter((c) => c.signal === 'page').length },
    byType, daily, byCompetitor, biggestMoves: moves, highlights,
    crawls: { count: runs.length, pages: runs.reduce((s, r) => s + r.pages, 0), withChanges: runs.filter((r) => r.changes > 0).length },
    hasSample: inCur.some((c) => c.sample),
    changes: inCur.map(({ ts, competitor, type, label, signal, sample }) => ({ ts, competitor, type, label, signal, sample: !!sample })),
  };
}
