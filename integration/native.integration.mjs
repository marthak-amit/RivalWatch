// Integration check for NATIVE mode: this Node server in front of a backend that serves the UI's own /api/* contract
// (Postgres + crawl worker + scheduler). Covers accounts, real product crawling of a stand-in Shopify-style shop,
// competitor settings, product search, change detection, reports, the admin panel and the /v1 proxy.
// Not part of `npm test` (needs live services). See integration/README.md.
import './shop.mjs'; // stand-in jewellery shop on 127.0.0.1:3998 (feeds, product pages, /__edit to change the catalogue)
import './magento.mjs'; // stand-in Magento store on 127.0.0.1:3997 (GraphQL catalogue + price REST APIs, token "good-token")
const N = process.env.NODE_URL || 'http://127.0.0.1:3000', SHOP = 'http://127.0.0.1:3998', MAG = 'http://127.0.0.1:3997';
const ADMIN = { email: process.env.ADMIN_EMAIL || 'boss@rivalwatch.test', password: process.env.ADMIN_PASSWORD || 'admin-pass-12345' };
let bad = 0, n = 0; const ok = (c, m) => { n++; console.log(c ? 'ok  ' : 'FAIL', m); if (!c) bad++; };
const jar = () => { let ck = ''; return async (m, p, b, extra = {}) => {
  const r = await fetch(N + p, { method: m, headers: { ...(m === 'GET' ? {} : { 'content-type': 'application/json' }), cookie: ck, ...extra }, body: m === 'GET' || b === undefined ? undefined : JSON.stringify(b), redirect: 'manual' });
  const sc = r.headers.getSetCookie?.() ?? []; for (const c of sc) ck = c.split(';')[0].startsWith('rw_session=;') ? '' : c.split(';')[0];
  const t = await r.text(); let d; try { d = JSON.parse(t); } catch { d = t; } return { status: r.status, data: d, headers: r.headers, cookies: sc }; }; };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const wait = async (fn, ms = 45000) => { const t0 = Date.now(); for (;;) { const v = await fn(); if (v) return v; if (Date.now() - t0 > ms) return null; await sleep(500); } };
const edit = (a) => fetch(`${SHOP}/__edit?action=${a}`);
const state = async (u) => (await u('GET', '/api/state')).data;
const idle = (u, id) => wait(async () => { const s = await state(u); const c = s.competitors.find((x) => x.id === id); return c && !c.crawling && !s.running && c.snapshot ? s : null; });
const crawl = async (u) => { const r = await u('POST', '/api/crawl', {}); await sleep(400); return r; };

// ---------------------------------------------------------------- pages and capability flags
await edit('reset');
let r = await fetch(N + '/theme.js'); ok(r.status === 200 && /javascript/.test(r.headers.get('content-type')), '/theme.js is served (light/dark toggle)');
r = await fetch(N + '/'); ok(r.status === 200 && /Know what your rivals changed/.test(await r.text()), 'landing page served');
r = await fetch(N + '/app', { redirect: 'manual' }); ok(r.status === 302 && r.headers.get('location') === '/login', '/app redirects when signed out');
r = await fetch(N + '/admin', { redirect: 'manual' }); ok(r.status === 302, '/admin redirects when signed out');
const anon = jar();
r = await anon('GET', '/api/plans'); ok(r.status === 200 && r.data.backend?.enabled === true && r.data.backend.planChanges === true && r.data.backend.demo === false, '/api/plans: native capability flags');
r = await anon('GET', '/api/session'); ok(r.status === 200 && r.data.user === null, '/api/session is null when signed out');
r = await anon('GET', '/api/state'); ok(r.status === 401, 'state needs a session');
r = await anon('POST', '/api/auth/login', { email: 'x@y.zz', password: 'abcdefgh' }, { 'content-type': 'text/plain' }); ok(r.status === 415, 'non-JSON write -> 415 (CSRF guard)');

// ---------------------------------------------------------------- accounts
const email = `native${Date.now()}@example.com`, u = jar();
r = await u('POST', '/api/auth/signup', { email: 'nope', password: 'x' }); ok(r.status === 400, 'signup validation: ' + r.data.error);
r = await u('POST', '/api/auth/signup', { email, password: 'password123', name: 'Dana Tester', plan: 'pro' });
ok(r.status === 200 && r.data.redirect === '/app' && r.data.user.name === 'Dana Tester' && r.data.user.plan === 'pro', 'signup stores name and plan');
ok(r.cookies.some((c) => /rw_session=/.test(c) && /HttpOnly/i.test(c) && /SameSite=Lax/i.test(c)), 'cookie passes through HttpOnly + SameSite=Lax');
ok(!JSON.stringify(r.data).includes('eyJ'), 'token is not in the response body');
r = await jar()('POST', '/api/auth/signup', { email, password: 'password123' }); ok(r.status === 400 && /already exists/.test(r.data.error), 'duplicate signup rejected');
r = await u('GET', '/api/me'); const me = r.data; ok(r.status === 200 && me.plan === 'pro' && me.credits === 2000 && /^rw_/.test(me.apiKey), `/api/me: pro plan, 2000 credits`);
r = await u('POST', '/api/me/rotate-key', {}); ok(r.status === 200 && r.data.apiKey !== me.apiKey && /^rw_/.test(r.data.apiKey), 'rotate key issues a new key'); const key = r.data.apiKey;
r = await u('POST', '/api/me/plan', { plan: 'business' }); ok(r.status === 200 && r.data.plan === 'business' && r.data.credits === 10000, 'plan change resets credits to the new allowance');
r = await u('POST', '/api/me/plan', { plan: 'pro' }); ok(r.data.credits === 2000, 'back to pro');
r = await jar()('POST', '/api/auth/login', { email, password: 'wrong-password' }); ok(r.status === 401 && /Incorrect/.test(r.data.error), 'wrong password -> 401');
r = await jar()('POST', '/api/auth/login', { email: 'a\u0000b@example.com', password: 'password123' }); ok(r.status === 400, 'NUL byte -> 400, not 500');
r = await jar()('GET', '/api/me'); ok(r.status === 401, 'no cookie -> 401');
{ let allOk = true; for (let i = 0; i < 12; i++) { const x = await jar()('POST', '/api/auth/login', { email, password: 'password123' }); if (x.status !== 200) allOk = false; } ok(allOk, '12 successful logins in a row are never rate-limited'); }

// ---------------------------------------------------------------- competitor with settings, real crawl
r = await u('POST', '/api/competitors', { url: 'http://10.0.0.5/' }); ok(r.status === 400 && /private|internal/i.test(r.data.error), 'private address blocked: ' + r.data.error);
r = await u('POST', '/api/competitors', { url: SHOP + '/', sort: 'cheapest' }); ok(r.status === 400 && /sort/.test(r.data.error), 'invalid sort rejected: ' + r.data.error);
r = await u('POST', '/api/competitors', { url: SHOP + '/', name: 'Lumen', maxProducts: 3, sort: 'price_desc', cron: '0 */6 * * *', crawlSettings: { browser: 'off', delay_sec: 0.2 } });
ok(r.status === 200 && r.data.ok && typeof r.data.id === 'string', 'add competitor with settings'); const id = r.data.id;
const c0 = r.data.competitor; ok(c0.maxProducts === 3 && c0.sort === 'price_desc' && c0.cron === '0 */6 * * *' && c0.crawlSettings.delay_sec === 0.2 && c0.crawlSettings.browser === 'off', 'settings are stored and returned');
r = await u('POST', '/api/competitors', { url: SHOP + '/other' }); ok(r.status === 400 && /Already monitoring/.test(r.data.error), 'duplicate site rejected');
let st = await idle(u, id); let c = st?.competitors.find((x) => x.id === id);
ok(!!c?.snapshot, 'first crawl finished through the worker');
ok(c?.snapshot.productCount === 3 && Object.keys(c.snapshot.products).length === 3, `product limit honoured (${c?.snapshot.productCount} of 6)`);
ok(['Solitaire Ring', 'Oval Halo Ring', 'Pearl Pendant'].every((t) => Object.values(c.snapshot.products).some((p) => p.name === t)), 'sort price_desc decides which products fill the limit');
ok(Object.values(c.snapshot.products).every((p) => /^£/.test(p.price) && p.amount > 0), 'prices are formatted in the shop currency (£)');
ok(c?.status === 'active' && c.enabled === true && c.platform === 'shopify' && c.crawling === false && c.error === null, `status ${c?.status}, platform ${c?.platform}`);
ok(c?.snapshot.profile?.headline === 'Fine jewellery, made to last' || typeof c?.snapshot.profile === 'object', 'company profile present');
ok(st.credits < 2000, `credits charged for the crawl (2000 -> ${st.credits})`);
r = await u('GET', `/api/competitors/${id}/categories`); ok(r.status === 200 && r.data.categories.some((x) => x.name === 'Rings'), 'categories: the site menu is offered for scoping');
r = await u('PATCH', `/api/competitors/${id}`, { categories: [{ name: 'Evil', url: 'https://evil.example/collections/x' }] }); ok(r.status === 400, 'a category from another site is rejected');
r = await u('PATCH', `/api/competitors/${id}`, { categories: [{ name: 'Rings', url: SHOP + '/collections/rings' }] }); ok(r.status === 200 && r.data.competitor.categories.length === 1, 'scope to a menu category');
r = await u('PATCH', `/api/competitors/${id}`, { categories: [], maxProducts: 50, sort: 'relevance', crawlSettings: { delay_sec: null, ai_extract: false } });
ok(r.status === 200 && r.data.competitor.maxProducts === 50 && r.data.competitor.categories.length === 0 && r.data.competitor.crawlSettings.ai_extract === false && r.data.competitor.crawlSettings.delay_sec !== 0.2, 'PATCH: scope cleared, limit raised, null resets a crawl setting');
r = await u('PATCH', `/api/competitors/${id}`, { cron: 'not a cron' }); ok(r.status === 400, 'invalid cron rejected');

// ---------------------------------------------------------------- crawl now: the rest of the catalogue
r = await crawl(u); ok(r.status === 200 && r.data.skipped === false, '/api/crawl queues the competitor');
st = await idle(u, id); c = st.competitors.find((x) => x.id === id);
ok(c.snapshot.productCount === 6, `all 6 products collected after raising the limit (${c.snapshot.productCount})`);

// ---------------------------------------------------------------- product search
const P = async (q) => (await u('GET', '/api/products?' + q)).data;
let pr = await P(''); ok(pr.total === 6 && pr.products.length === 6, 'products: 6 in total');
ok(pr.facets.categories.includes('Rings') && pr.facets.categories.includes('Earrings') && pr.facets.currencies.includes('GBP'), `facets: ${pr.facets.categories.join('/')}`);
ok((await P('category=Earrings')).total === 2, 'filter by category');
ok((await P('min_price=400&max_price=800')).products.every((p) => p.price >= 400 && p.price <= 800), 'filter by price range');
pr = await P('on_sale=true'); ok(pr.total === 1 && pr.products[0].wasPrice && pr.products[0].discountPct === 20, 'on sale: was price and discount %');
ok((await P('q=ring')).products.every((p) => /ring/i.test(p.title)), 'search matches word starts');
pr = await P('sort=price_asc'); ok(pr.products[0].price === 80 && pr.products.at(-1).price === 1200, 'sort price low to high');
pr = await P('limit=2&offset=2'); ok(pr.products.length === 2 && pr.total === 6 && pr.offset === 2, 'pagination');
ok((await P('competitor=' + id)).total === 6, 'filter by competitor id');
r = await u('GET', '/api/products?sort=bogus'); ok(r.status === 400, 'bad sort -> 400');
r = await jar()('GET', '/api/products'); ok(r.status === 401, 'products need a session');

// ---------------------------------------------------------------- change detection
await edit('drop'); await edit('sale'); await edit('oos'); await edit('add');
r = await crawl(u); st = await idle(u, id);
const types = new Set(st.changes.map((x) => x.type)); const ch = (t) => st.changes.find((x) => x.type === t);
const drop = st.changes.find((x) => x.type === 'price_change' && /Stud Earrings/.test(x.label));
ok(drop?.pct === -20 && drop.from === '£300.00' && drop.to === '£240.00', 'price drop detected: ' + drop?.label);
ok(!!ch('on_sale') && ch('on_sale').pct === -25, 'on sale detected: ' + ch('on_sale')?.label);
ok(!!ch('out_of_stock') && /Pearl Pendant/.test(ch('out_of_stock').label), 'out of stock detected');
ok(!!ch('product_added') && /Emerald Cocktail Ring/.test(ch('product_added').label), 'new product detected');
ok(types.has('new_category') || types.has('new_promo') || types.has('page_changed'), `page-level changes detected (${[...types].filter((t) => /category|promo|page/.test(t)).join(', ')})`);
ok(st.changes.every((x) => x.label && x.competitorId === id && x.ts), 'every change has a ready-to-show label');
ok(st.digest?.actions?.length >= 3 && st.mode === 'rules', `digest has ${st.digest?.actions?.length} actions (mode ${st.mode})`);
await edit('restock'); await crawl(u); st = await idle(u, id); ok(st.changes.some((x) => x.type === 'back_in_stock'), 'back in stock detected');
await sleep(300); st = await state(u); ok(st.changes.length === new Set(st.changes.map((x) => x.id)).size, 'change ids are unique');
const w = (await u('GET', '/api/reports?period=week&competitor=' + id)).data;
ok(w.total >= 5 && w.byType && w.daily?.length === 7 && w.byCompetitor?.length >= 1, `weekly report: ${w.total} changes, ${Object.keys(w.byType).length} types`);
r = await u('POST', '/api/reports/summary', { period: 'week', competitor: id }); ok(r.status === 200 && r.data.actions.length >= 3 && r.data.source === 'rules', 'AI summary (rules) returns actions');
r = await u('GET', '/api/reports?period=month'); ok(r.status === 200 && r.data.total >= w.total, 'monthly report');

// ---------------------------------------------------------------- pause / resume / remove
r = await u('PATCH', `/api/competitors/${id}`, { enabled: false }); ok(r.status === 200 && r.data.enabled === false && r.data.competitor.status === 'paused', 'disable pauses monitoring');
r = await u('POST', '/api/crawl', {}); ok(r.data.skipped === true, 'a paused competitor is skipped by crawl now');
r = await u('PATCH', `/api/competitors/${id}`, { enabled: true }); ok(r.status === 200 && r.data.enabled === true, 'enable resumes (and crawls)');
st = await idle(u, id);

// ---------------------------------------------------------------- crawl API and key
r = await jar()('GET', '/v1/credits/balance', undefined, { 'x-api-key': key }); ok(r.status === 200 && r.data.success === true, 'GET /v1/credits/balance via the proxy');
r = await jar()('GET', '/v1/web/scrape?url=' + encodeURIComponent(SHOP + '/'), undefined, { 'x-api-key': key }); ok(r.status === 200 && /Handmade fine jewellery/.test(JSON.stringify(r.data)), 'scrape returns the page meta description');
r = await jar()('GET', '/v1/credits/balance', undefined, { 'x-api-key': 'rw_wrong' }); ok(r.status === 401, 'bad API key -> 401');

// ---------------------------------------------------------------- our store (Magento): connect, sync, edit
await fetch(MAG + '/__edit?action=reset');
const magPrice = async (sku) => (await (await fetch(MAG + '/__state')).json())[sku];
r = await u('GET', '/api/store'); ok(r.status === 200 && r.data.store === null, 'no store connected yet');
r = await u('PUT', '/api/store', { url: SHOP + '/' }); ok(r.status === 400 && typeof r.data.error === 'string', 'a non-Magento URL is refused with a message: ' + r.data.error);
r = await u('PUT', '/api/store', { url: MAG, token: 'good-token' }); ok(r.status === 200 && r.data.store.platform === 'magento' && r.data.store.hasToken === true, 'connect the store');
ok(!JSON.stringify(r.data).includes('good-token'), 'the token is never returned');
const synced = async (n) => wait(async () => { const x = (await u('GET', '/api/store')).data.store; return x && !x.syncing && x.productCount === n ? x : null; });
let store = await synced(7); ok(store?.productCount === 7 && store.currency === 'USD' && store.name === 'Louped Demo' && store.status === 'connected', `first sync: ${store?.productCount} products, ${store?.currency}`);
st = await state(u); ok(st.store?.id === store.id, 'state carries the store');
pr = await P('competitor=ours'); ok(pr.total === 7 && pr.products.every((p) => p.sku) && pr.products.some((p) => p.sku === 'SOLITAIRE-RING' && p.price === 1500), 'our products have SKUs and prices');
ok((await P('competitor=ours&on_sale=true')).products.map((p) => p.sku).join() === 'PAVE-BAND', 'our sale price is detected (PAVE-BAND)');
await fetch(MAG + '/__edit?action=reprice'); await fetch(MAG + '/__edit?action=sale'); await fetch(MAG + '/__edit?action=oos'); await fetch(MAG + '/__edit?action=add');
r = await u('POST', '/api/store/sync', {}); ok(r.status === 200, 'sync now'); store = await synced(8); ok(store?.productCount === 8, 'resync picks up the new product');
pr = await P('competitor=ours&q=emerald'); ok(pr.total === 1, 'new product is searchable'); ok((await P('competitor=ours&in_stock=false')).total >= 0 && (await P('competitor=ours&sort=price_desc')).products[0].sku === 'SAPPHIRE-RING', 'sort works on our catalogue');
r = await u('PATCH', '/api/store', { enabled: false }); ok(r.status === 200 && r.data.store.enabled === false, 'pause the sync');
r = await u('PATCH', '/api/store', { enabled: true, cron: '0 */6 * * *' }); ok(r.data.store.enabled === true && r.data.store.cron === '0 */6 * * *', 'resume and change the schedule');
r = await u('PATCH', '/api/store', { token: '' }); ok(r.data.store.hasToken === false, 'a token can be removed');
r = await u('PATCH', '/api/store', { token: 'good-token' }); ok(r.data.store.hasToken === true, 'and set again');
r = await u('PATCH', '/api/store', { cron: 'nope' }); ok(r.status === 400, 'bad schedule rejected');

// ---------------------------------------------------------------- comparison (M6)
r = await u('GET', '/api/comparison'); ok(r.status === 200 && r.data.ours?.currency === 'USD' && Array.isArray(r.data.rows), 'comparison answers once the store is connected');
ok(r.data.notes.some((n) => /GBP/.test(n)), 'it says a GBP rate is needed instead of guessing: ' + r.data.notes[0]);
ok(r.data.matches.length >= 3 && r.data.matches.every((m) => m.gapPct === null), 'shared SKUs are matched but not compared without a rate');
r = await u('PATCH', '/api/comparison/settings', { minGroupSize: 1, minPrice: 10, fxRates: { GBP: 1.27 } }); ok(r.status === 200 && r.data.fx_rates?.GBP === 1.27 && r.data.min_group_size === 1, 'comparison settings with a currency rate');
r = await u('PATCH', '/api/comparison/settings', { fxRates: { gbp: 1 } }); ok(r.status === 400, 'a bad currency code is rejected');
let cmp = (await u('GET', '/api/comparison')).data;
ok(cmp.rows.length >= 2 && cmp.rows.every((x) => x.ours && x.competitors.length), `groups are compared (${cmp.rows.map((x) => x.label).join(', ')})`);
ok(cmp.rows.some((x) => x.competitors.some((c) => c.comparable && typeof c.gapPct === 'number')), 'gaps are numbers once there is a rate');
const solit = cmp.matches.find((m) => /Solitaire/.test(m.title)); ok(solit && typeof solit.gapPct === 'number' && solit.theirs.priceInOurCurrency > 0, 'shared SKU gap: ' + JSON.stringify([solit?.ours.price, solit?.theirs.price, solit?.gapPct]));
ok(cmp.notes.length === 0, 'no notes left after the rate');
for (const g of ['type,metal', 'type,stone', 'type,metal,stone', 'type,carat']) { r = await u('GET', '/api/comparison?group_by=' + g); if (r.status !== 200) ok(false, 'group_by ' + g); }
ok(true, 'every group-by variant answers'); r = await u('GET', '/api/comparison?group_by=bogus'); ok(r.status === 400, 'bad group_by -> 400');

// ---------------------------------------------------------------- our prices in Magento (M7)
r = await u('GET', '/api/prices'); ok(r.status === 200 && r.data.settings.maxChangePct === 50 && r.data.settings.adminOnly === false && r.data.changes.length === 0, 'price guardrail defaults: ' + JSON.stringify(r.data.settings));
r = await u('POST', '/api/prices', { sku: 'NOPE-1', newPrice: 10 }); ok(r.status === 404, 'unknown SKU -> 404: ' + r.data.error);
r = await u('POST', '/api/prices', { sku: 'SOLITAIRE-RING', newPrice: 0 }); ok(r.status === 400, 'a zero price is rejected');
r = await u('POST', '/api/prices', { sku: 'SOLITAIRE-RING', newPrice: 99999 }); ok(r.status === 400 && /50|too|change/i.test(r.data.error), 'a 60x jump trips the guardrail: ' + r.data.error);
r = await u('POST', '/api/prices', { sku: 'SOLITAIRE-RING', newPrice: 1450 }); const ch1 = r.data.change, pv = r.data.preview;
ok(r.status === 200 && ch1.status === 'pending' && ch1.oldPrice === 1500 && ch1.newPrice === 1450, 'request: pending, nothing applied yet'); ok((await magPrice('SOLITAIRE-RING')) === 1500, 'Magento still has the old price');
ok(pv.priceSource === 'magento' && pv.cost === 900 && pv.marginBefore === 40 && pv.canApply === true, `preview: live price, cost ${pv.cost}, margin ${pv.marginBefore}% -> ${pv.marginAfter}%`);
ok(pv.position && Array.isArray(pv.position.competitors), 'preview shows where this puts us against competitors');
r = await u('POST', `/api/prices/${ch1.id}/apply`, {}); ok(r.status === 200 && r.data.change.status === 'applied' && r.data.change.appliedBy === email, 'apply'); ok((await magPrice('SOLITAIRE-RING')) === 1450, 'Magento now has 1450 (written first)');
ok((await P('competitor=ours&q=solitaire')).products[0].price === 1450, 'our catalogue copy follows');
r = await u('POST', `/api/prices/${ch1.id}/apply`, {}); ok(r.status === 409, 'an applied change can’t be applied twice');
r = await u('POST', `/api/prices/${ch1.id}/revert`, {}); ok(r.status === 200 && r.data.change.status === 'applied' && r.data.change.newPrice === 1500 && r.data.change.reverts === ch1.id, 'revert puts 1500 back as a change of its own'); ok((await magPrice('SOLITAIRE-RING')) === 1500, 'Magento is back to 1500');
r = await u('POST', '/api/prices', { sku: 'PAVE-BAND', newPrice: 110 }); ok(r.status === 200 && r.data.preview.salePrice === 95 && r.data.preview.shownAfter === 95, 'a sale price stays: shoppers still pay 95');
const chSale = r.data.change; r = await u('POST', `/api/prices/${chSale.id}/cancel`, {}); ok(r.status === 200 && r.data.change.status === 'cancelled', 'cancel a pending change'); ok((await magPrice('PAVE-BAND')) === 120, 'cancel changed nothing in Magento (regular price still 120)');
r = await u('PATCH', '/api/prices/settings', { minMarginPct: 50 }); ok(r.status === 200 && r.data.minMarginPct === 50, 'raise the minimum margin');
r = await u('POST', '/api/prices', { sku: 'SOLITAIRE-RING', newPrice: 1200 }); ok(r.status === 400 && /margin/i.test(r.data.error), 'margin guardrail (cost 900): ' + r.data.error);
await u('PATCH', '/api/prices/settings', { minMarginPct: 0, maxChangePct: 5 }); r = await u('POST', '/api/prices', { sku: 'SOLITAIRE-RING', newPrice: 1200 }); ok(r.status === 400, 'max-change guardrail (5%): ' + r.data.error);
await u('PATCH', '/api/prices/settings', { maxChangePct: 50 });
await u('PATCH', '/api/store', { token: 'wrong-token' }); r = await u('POST', '/api/prices', { sku: 'STUD-EARRINGS', newPrice: 320 }); const chBad = r.data.change;
ok(r.status === 200 && r.data.preview.priceSource === 'catalog' && r.data.preview.warnings.length > 0, 'with a token Magento refuses, the preview falls back to the synced price and warns: ' + r.data.preview?.warnings?.[0]);
r = await u('POST', `/api/prices/${chBad.id}/apply`, {}); ok(r.status === 502 && /authoriz|HTTP 401/i.test(r.data.error), 'Magento refuses -> 502 with its own error: ' + r.data.error); ok((await magPrice('STUD-EARRINGS')) === 310, 'nothing changed in Magento');
r = await u('GET', '/api/prices?status=failed'); ok(r.data.changes.length === 1 && r.data.changes[0].status === 'failed' && /authoriz/i.test(r.data.changes[0].error), 'the failed change is kept with the error');
await u('PATCH', '/api/store', { token: 'good-token' });
r = await u('GET', '/api/prices'); ok(r.data.changes.length >= 4, `price history lists ${r.data.changes.length} changes`); r = await u('GET', '/api/prices?status=bogus'); ok(r.status === 400, 'bad status filter -> 400');

// ---------------------------------------------------------------- remove
r = await u('DELETE', `/api/competitors/${id}`, {}); ok(r.status === 200, 'remove competitor');
st = await state(u); ok(!st.competitors.some((x) => x.id === id) && !st.changes.some((x) => x.competitorId === id), 'its data is gone');

// ---------------------------------------------------------------- admin panel
const a = jar();
r = await a('POST', '/api/auth/login', ADMIN); ok(r.status === 200 && r.data.redirect === '/admin', 'admin login -> /admin');
r = await fetch(N + '/admin', { headers: { cookie: r.cookies[0].split(';')[0] }, redirect: 'manual' }); ok(r.status === 200, '/admin page opens for the admin');
r = await a('GET', '/api/admin/stats'); ok(r.status === 200 && r.data.total >= 2 && r.data.byPlan, 'admin stats include the plan mix');
r = await a('GET', '/api/admin/users?q=' + encodeURIComponent(email)); const row = r.data.users?.[0];
ok(row?.email === email && row.plan === 'pro' && row.name === 'Dana Tester' && typeof row.credits === 'number', 'admin list shows name, plan and credits');
// ---- the agency applies this client's price changes
r = await a('GET', `/api/admin/users/${row.id}/store`); ok(r.status === 200 && r.data.store?.productCount === 8, 'admin sees the client’s store');
r = await a('PATCH', `/api/admin/users/${row.id}/prices/settings`, { adminOnly: true, maxChangePct: 40 }); ok(r.status === 200 && r.data.adminOnly === true && r.data.maxChangePct === 40, 'admin makes the agency the one who applies, and tightens the limit');
r = await u('PATCH', '/api/prices/settings', { maxChangePct: 10 }); ok(r.status === 403, 'the client can no longer change the guardrails: ' + r.data.error);
r = await u('POST', '/api/prices', { sku: 'GOLD-HOOP-EARRINGS', newPrice: 150 }); const chA = r.data.change; ok(r.status === 200 && chA.status === 'pending' && r.data.preview.settings.adminOnly === true, 'the client’s request waits for the agency');
r = await u('POST', `/api/prices/${chA.id}/apply`, {}); ok(r.status === 403, 'the client can’t apply it: ' + r.data.error);
r = await a('GET', '/api/admin/prices?status=pending'); ok(r.status === 200 && r.data.changes.some((c) => c.id === chA.id && c.client === email), 'it shows in the admin’s waiting-for-approval list');
r = await a('GET', `/api/admin/prices?user_id=${row.id}`); ok(r.data.changes.length >= 5, 'the client’s whole price history is visible to the admin');
r = await a('POST', `/api/admin/prices/${chA.id}/apply`, {}); ok(r.status === 200 && r.data.change.status === 'applied' && r.data.change.appliedBy === ADMIN.email && r.data.change.requestedBy === email, 'admin applies it (requested by the client, applied by the admin)');
ok((await magPrice('GOLD-HOOP-EARRINGS')) === 150, 'Magento has the new price');
r = await a('POST', `/api/admin/users/${row.id}/prices`, { sku: 'GOLD-HOOP-EARRINGS', newPrice: 155 }); const chB = r.data.change; ok(r.status === 200 && chB.status === 'pending' && r.data.preview.currentPrice === 150, 'admin requests a change for the client');
r = await a('POST', `/api/admin/prices/${chB.id}/cancel`, {}); ok(r.status === 200 && r.data.change.status === 'cancelled', 'and cancels it');
r = await a('POST', `/api/admin/prices/${chA.id}/revert`, {}); ok(r.status === 200 && r.data.change.status === 'applied' && r.data.change.newPrice === 160, 'admin reverts: applied at once'); ok((await magPrice('GOLD-HOOP-EARRINGS')) === 160, 'Magento is back to 160');
r = await a('PATCH', `/api/admin/users/${row.id}/store`, { enabled: false }); ok(r.status === 200 && r.data.store.enabled === false, 'admin pauses the client’s sync'); await a('PATCH', `/api/admin/users/${row.id}/store`, { enabled: true });
r = await a('POST', `/api/admin/users/${row.id}/store/sync`, {}); ok(r.status === 200, 'admin syncs the client’s store now');
await a('PATCH', `/api/admin/users/${row.id}/prices/settings`, { adminOnly: false, maxChangePct: 50 });
r = await a('DELETE', `/api/admin/users/${row.id}/store`, {}); ok(r.status === 200, 'admin disconnects the client’s store'); r = await u('GET', '/api/store'); ok(r.data.store === null, 'the client’s store is gone');
r = await u('GET', '/api/comparison'); ok(r.status === 200 && r.data.ours === null && r.data.notes.length > 0, 'comparison without a store explains how to start');
r = await a('PATCH', '/api/admin/users/' + row.id, { plan: 'starter' }); ok(r.status === 200 && r.data.plan === 'starter', 'admin changes a plan');
r = await a('POST', `/api/admin/users/${row.id}/reset-credits`, {}); ok(r.status === 200 && r.data.credits === 300, 'admin resets credits to the plan allowance');
r = await a('POST', `/api/admin/users/${row.id}/reset-password`, { password: 'new-password-1' }); ok(r.status === 200, 'admin sets a password');
ok((await u('GET', '/api/me')).status === 401, 'the old session ended with the password reset');
r = await jar()('POST', '/api/auth/login', { email, password: 'new-password-1' }); ok(r.status === 200, 'login with the new password');
r = await a('PATCH', '/api/admin/users/' + row.id, { status: 'suspended' }); ok(r.status === 200 && r.data.status === 'suspended', 'admin suspends');
r = await jar()('POST', '/api/auth/login', { email, password: 'new-password-1' }); ok(r.status === 403 && /suspended/i.test(r.data.error), 'suspended user: 403 "' + r.data.error + '"');
r = await a('POST', '/api/admin/users', { email: `made${Date.now()}@example.com`, name: 'Made By Admin', password: 'password123', plan: 'business' }); ok(r.status === 200 && r.data.plan === 'business', 'admin creates a user');
const made = r.data.id; r = await a('POST', '/api/admin/users', { email: `adm${Date.now()}@example.com`, password: 'password123', role: 'admin' }); ok(r.status === 403, 'admin cannot create admins here');
r = await a('DELETE', '/api/admin/users/' + made, {}); ok(r.status === 200, 'admin deletes a user');
r = await a('DELETE', '/api/admin/users/' + row.id, {}); ok(r.status === 200, 'admin deletes the test user');
r = await a('GET', '/api/admin/audit'); ok(r.data.audit.length >= 4 && r.data.audit.some((x) => x.action === 'update_user' && /suspended/.test(x.detail)), `audit log has ${r.data.audit.length} entries`);
r = await a('POST', '/api/auth/logout', {}); ok(r.status === 200, 'admin logout'); r = await a('GET', '/api/admin/stats'); ok(r.status === 401, 'admin session ended');

console.log(`\n${bad ? bad + ' FAILED of ' + n : 'all ' + n + ' passed'}`);
process.exit(bad ? 1 : 0);
