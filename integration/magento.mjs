// Stand-in Magento 2 store for end-to-end tests (test harness only). Port 3997.
//   GET  /graphql?query=...      storeConfig and the paged public catalog (what the backend's sync reads)
//   POST /rest/V1/products/base-prices-information | cost-information | base-prices   (what price changes use; needs the token)
//   GET  /__edit?action=...      change the catalogue (reprice|sale|oos|add|reset); GET /__state shows live prices
// The accepted integration token is MAGENTO_TOKEN (default "good-token"); any other token gets Magento's 401 for missing permission.
import http from 'node:http';

const PORT = +process.env.MAGENTO_PORT || 3997, TOKEN = process.env.MAGENTO_TOKEN || 'good-token';
const when = (daysAgo) => new Date(Date.now() - daysAgo * 864e5).toISOString().replace('T', ' ').slice(0, 19);
const item = (sku, name, price, { cat = 'Rings', cost = null, was = null, stock = true, age = 90 } = {}) => ({ sku, name, price, cost, was, cat, stock, age });
const base = () => [
  item('SOLITAIRE-RING', 'Solitaire Ring', 1500, { cost: 900 }), item('PAVE-BAND', 'Pave Band', 95, { was: 120, cost: 40 }), item('STUD-EARRINGS', 'Stud Earrings', 340, { cat: 'Earrings', cost: 150 }),
  item('GOLD-HOOP-EARRINGS', 'Gold Hoop Earrings', 160, { cat: 'Earrings', cost: 70 }), item('OPAL-PENDANT', 'Opal Pendant', 410, { cat: 'Necklaces', cost: 180 }),
  item('SAPPHIRE-RING', 'Sapphire Ring', 2100, { cost: 1100 }), item('TENNIS-BRACELET', 'Tennis Bracelet', 980, { cat: 'Bracelets', cost: 520 }),
];
let items = base();
const EDITS = {
  reprice: () => { items.find((i) => i.sku === 'STUD-EARRINGS').price = 310; },
  sale: () => { Object.assign(items.find((i) => i.sku === 'OPAL-PENDANT'), { price: 349, was: 410 }); },
  oos: () => { items.find((i) => i.sku === 'TENNIS-BRACELET').stock = false; },
  add: () => { items.push(item('EMERALD-RING', 'Emerald Ring', 1750, { cost: 800, age: 0 })); },
  reset: () => { items = base(); },
};
const j = (res, status, o) => { res.writeHead(status, { 'content-type': 'application/json' }); res.end(JSON.stringify(o)); };
const gql = (i) => ({ sku: i.sku, name: i.name, url_key: i.sku.toLowerCase(), url_suffix: '.html', created_at: when(i.age), updated_at: when(0), stock_status: i.stock ? 'IN_STOCK' : 'OUT_OF_STOCK',
  meta_title: `${i.name} | Louped Demo`, meta_description: `Shop the ${i.name} in 18k gold with a natural diamond.`, categories: [{ name: 'Jewelry', level: 2 }, { name: i.cat, level: 3 }], small_image: { url: `http://127.0.0.1:${PORT}/img/${i.sku}.jpg` },
  price_range: { minimum_price: { regular_price: { value: i.was ?? i.price, currency: 'USD' }, final_price: { value: i.price, currency: 'USD' } } } });
const NOAUTH = { message: "The consumer isn't authorized to access %resources.", parameters: { resources: 'Magento_Catalog::catalog' } };

http.createServer((req, res) => {
  const u = new URL(req.url, 'http://x'), p = u.pathname;
  let raw = ''; req.on('data', (d) => (raw += d));
  req.on('end', () => {
    if (p === '/__edit') { const f = EDITS[u.searchParams.get('action')]; if (!f) return j(res, 400, { error: 'unknown action' }); f(); return j(res, 200, { ok: true, products: items.length }); }
    if (p === '/__state') return j(res, 200, Object.fromEntries(items.map((i) => [i.sku, i.was ?? i.price]))); // the regular (base) price
    if (p === '/graphql') {
      const q = u.searchParams.get('query') || '';
      if (/Authorization/i.test(Object.keys(req.headers).join()) && req.headers.authorization) return j(res, 200, { errors: [{ message: 'Composite reader could not read a token' }] }); // like a real store when a token is sent to GraphQL
      if (q.includes('storeConfig')) return j(res, 200, { data: { storeConfig: { store_name: 'Louped Demo', base_currency_code: 'USD', base_url: `http://127.0.0.1:${PORT}/`, product_url_suffix: '.html' } } });
      if (q.includes('products(')) { const v = JSON.parse(u.searchParams.get('variables') || '{}'), size = v.size || 100, page = v.page || 1; const slice = items.slice((page - 1) * size, page * size);
        return j(res, 200, { data: { products: { total_count: items.length, page_info: { total_pages: Math.max(1, Math.ceil(items.length / size)), current_page: page }, items: slice.map(gql) } } }); }
      return j(res, 200, { errors: [{ message: 'unsupported query' }] });
    }
    if (p.startsWith('/rest/V1/products/') && req.method === 'POST') {
      if (req.headers.authorization !== `Bearer ${TOKEN}`) return j(res, 401, NOAUTH);
      const b = raw ? JSON.parse(raw) : {}, op = p.split('/').pop();
      if (op === 'base-prices-information') return j(res, 200, (b.skus || []).map((s) => items.find((i) => i.sku === s)).filter(Boolean).map((i) => ({ price: String(i.was ?? i.price), store_id: 0, sku: i.sku })));
      if (op === 'cost-information') return j(res, 200, (b.skus || []).map((s) => items.find((i) => i.sku === s)).filter((i) => i?.cost != null).map((i) => ({ cost: String(i.cost), store_id: 0, sku: i.sku })));
      if (op === 'base-prices') {
        const errs = [];
        for (const pr of b.prices || []) { const it = items.find((i) => i.sku === pr.sku); if (!it) { errs.push({ message: 'Requested product doesn\'t exist: %sku', parameters: { sku: pr.sku } }); continue; }
          // the regular (base) price changes; a sale price stays
          if (it.was != null) it.was = pr.price; else it.price = pr.price; }
        return j(res, 200, errs);
      }
    }
    j(res, 404, { message: 'not found' });
  });
}).listen(PORT, '127.0.0.1', () => console.log(`Stand-in Magento on http://127.0.0.1:${PORT}`));
