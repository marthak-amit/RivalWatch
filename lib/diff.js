// Compare two extracted snapshots -> list of typed changes.
export function diff(prev, next) {
  const changes = [];
  for (const [k, p] of Object.entries(next.products)) {
    const o = prev.products[k];
    if (!o) changes.push({ signal: 'product', type: 'product_added', name: p.name, price: p.price });
    else if (o.amount !== p.amount) {
      const pct = o.amount ? Math.round(((p.amount - o.amount) / o.amount) * 1000) / 10 : null;
      changes.push({ signal: 'product', type: 'price_change', name: p.name, from: o.price, to: p.price, pct });
    }
  }
  for (const [k, o] of Object.entries(prev.products)) {
    if (!next.products[k]) changes.push({ signal: 'product', type: 'product_removed', name: o.name, price: o.price });
  }
  for (const p of next.promotions) if (!prev.promotions.includes(p)) changes.push({ signal: 'page', type: 'new_promo', text: p });
  for (const p of prev.promotions) if (!next.promotions.includes(p)) changes.push({ signal: 'page', type: 'promo_ended', text: p });
  for (const p of next.pages) if (!prev.pages.includes(p)) changes.push({ signal: 'page', type: 'new_page', path: p });
  for (const p of prev.pages) if (!next.pages.includes(p)) changes.push({ signal: 'page', type: 'page_removed', path: p });
  return changes;
}

export function describe(c) {
  switch (c.type) {
    case 'price_change': return `${c.name}: ${c.from} → ${c.to}${c.pct != null ? ` (${c.pct > 0 ? '+' : ''}${c.pct}%)` : ''}`;
    case 'product_added': return `New product: ${c.name} at ${c.price}`;
    case 'product_removed': return `Product removed: ${c.name} (was ${c.price})`;
    case 'new_promo': return `New promotion: “${c.text}”`;
    case 'promo_ended': return `Promotion ended: “${c.text}”`;
    case 'new_page': return `New page: ${c.path}`;
    case 'page_removed': return `Page removed: ${c.path}`;
    default: return JSON.stringify(c);
  }
}
