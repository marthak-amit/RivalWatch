// Stand-in competitor jewellery shop (Shopify-style feeds) for testing the product crawler end to end. Test harness only.
// Port 3998. GET /__edit?action=drop|rise|sale|oos|restock|add|reset changes the catalogue so the next crawl detects it.
import http from 'node:http';

const PORT = +process.env.SHOP_PORT || 3998;
const day = 864e5;
const mk = (handle, price, { compare = null, kind = 'Rings', available = true, age = 60, vendor = 'Lumen' } = {}) => ({
  title: handle.replace(/-/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()), handle, product_type: kind, vendor,
  published_at: new Date(Date.now() - age * day).toISOString(), tags: ['gold', 'diamond'],
  variants: [{ sku: handle.toUpperCase(), price: String(price), compare_at_price: compare && String(compare), available }],
});
const base = () => [
  mk('oval-halo-ring', 750, { age: 2 }), mk('pave-band', 80, { compare: 100 }), mk('stud-earrings', 300, { kind: 'Earrings' }),
  mk('gold-hoop-earrings', 120, { kind: 'Earrings' }), mk('pearl-pendant', 450, { kind: 'Necklaces' }), mk('solitaire-ring', 1200),
];
let items = base(), promo = 'Free shipping over $500', cats = [{ title: 'Classics', handle: 'classics', published_at: new Date(Date.now() - 400 * day).toISOString() }];
const find = (h) => items.find((p) => p.handle === h);
const EDITS = {
  drop: () => { find('stud-earrings').variants[0].price = '240'; },
  rise: () => { find('solitaire-ring').variants[0].price = '1350'; },
  sale: () => { Object.assign(find('gold-hoop-earrings').variants[0], { price: '90', compare_at_price: '120' }); },
  oos: () => { find('pearl-pendant').variants[0].available = false; },
  restock: () => { find('pearl-pendant').variants[0].available = true; },
  add: () => { items.push(mk('emerald-cocktail-ring', 980, { age: 0 })); cats = [...cats, { title: 'Lab-Grown', handle: 'lab-grown', published_at: new Date().toISOString() }]; promo = '20% off earrings this week'; },
  reset: () => { items = base(); promo = 'Free shipping over $500'; cats = cats.slice(0, 1); },
};
const json = (res, o) => { res.writeHead(200, { 'content-type': 'application/json' }); res.end(JSON.stringify(o)); };

http.createServer((req, res) => {
  const u = new URL(req.url, 'http://x'), p = u.pathname;
  if (p === '/__edit') { const f = EDITS[u.searchParams.get('action')]; if (!f) { res.writeHead(400); return res.end('unknown action'); } f(); return json(res, { ok: true, products: items.length }); }
  if (p === '/robots.txt') { res.writeHead(200, { 'content-type': 'text/plain' }); return res.end('User-agent: *\nDisallow: /checkout\n'); }
  if (p === '/products.json') return json(res, { products: u.searchParams.get('page') > 1 ? [] : items });
  if (p === '/collections.json') return json(res, { collections: cats });
  if (p === '/meta.json') return json(res, { name: 'Lumen', currency: 'GBP' });
  const m = p.match(/^\/products\/([\w-]+)$/);
  if (m && find(m[1])) { res.writeHead(200, { 'content-type': 'text/html' }); const t = find(m[1]).title; return res.end(`<html><head><title>${t} | Lumen</title><meta name="description" content="Shop ${t}."><meta property="og:title" content="${t}"></head><body><h1>${t}</h1></body></html>`); }
  if (p === '/') {
    res.writeHead(200, { 'content-type': 'text/html' });
    return res.end(`<html><head><title>Lumen Fine Jewellery</title><meta name="description" content="Handmade fine jewellery, ethically sourced."><script src="https://cdn.shopify.com/s/t.js"></script></head><body><header><nav><a href="/collections/rings">Rings</a><a href="/collections/earrings">Earrings</a></nav></header><h1>Fine jewellery, made to last</h1><p>${promo}</p><footer><a href="mailto:hello@lumen.example">hello@lumen.example</a> <a href="https://instagram.com/lumen">Instagram</a></footer></body></html>`);
  }
  res.writeHead(404, { 'content-type': 'text/plain' }); res.end('not found');
}).listen(PORT, '127.0.0.1', () => console.log(`Stand-in jewellery shop on http://127.0.0.1:${PORT}`));
