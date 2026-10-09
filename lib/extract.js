// Turn page markdown into the two signals RivalWatch tracks:
//   1. products + prices   2. pages (links) + promotions
const PRICE = /[$€£]\s?\d[\d,]*(?:\.\d+)?(?:\s?\/\s?\w+)?/;
const PROMO = /(\d+\s*%\s*off|\boff\b|save\s+\d|promo|coupon|discount|limited[- ]time|\bsale\b|use code|free (?:trial|month|shipping|setup))/i;

const clean = (s) => s.replace(/\[([^\]]+)\]\([^)]*\)/g, '$1').replace(/^\s*[-*]\s+/, '').replace(/[*_`>#|]/g, ' ').replace(/\s+/g, ' ').trim();

export function extract(markdown, baseUrl) {
  const products = {};
  const promotions = [];
  for (const raw of markdown.split('\n')) {
    const line = raw.trim();
    if (!line || /^#{1,3}\s/.test(line) && !PRICE.test(line)) continue;
    if (PROMO.test(line)) {
      const text = clean(line);
      if (text && !promotions.includes(text)) promotions.push(text);
      continue;
    }
    const m = line.match(PRICE);
    if (!m) continue;
    const name = clean(line.slice(0, m.index)).replace(/[-—:–]+$/, '').trim();
    if (!name) continue;
    products[name.toLowerCase()] = {
      name,
      price: m[0].replace(/\s/g, ''),
      amount: Number(m[0].replace(/[^\d.]/g, '')),
    };
  }

  const pages = new Set();
  const base = new URL(baseUrl);
  const root = base.pathname.replace(/\/$/, '');
  const rel = (p) => p.replace(/\/$/, '').slice(p.startsWith(root) ? root.length : 0) || '/';
  for (const [, , href] of markdown.matchAll(/\[([^\]]+)\]\(([^)\s]+)\)/g)) {
    try {
      const u = new URL(href, base);
      if (u.origin === base.origin) pages.add(rel(u.pathname));
    } catch { /* ignore malformed */ }
  }
  return { products, promotions, pages: [...pages].sort() };
}
