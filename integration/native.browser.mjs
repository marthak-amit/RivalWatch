// Browser test (Playwright): dashboard against the real backend in native mode: add a competitor with settings, crawl progress and
// catalog size on the card, the Settings dialog, the Products page and its filters, and the new change types.
// Needs the stack from integration/README.md plus the stand-in shop (node integration/shop.mjs). Run: npm run test:browser
import os from 'node:os';
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const B = 'http://localhost:3000', SHOP = 'http://127.0.0.1:3998', S = process.env.SHOTS_DIR || os.tmpdir();
let bad = 0, n = 0; const ok = (c, m) => { n++; if (!c) { bad++; console.error('FAIL', m); } else console.log('ok  ', m); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  await fetch(SHOP + '/__edit?action=reset');
  const b = await chromium.launch({ executablePath: process.env.CHROMIUM || undefined });
  const ctx = await b.newContext({ viewport: { width: 1400, height: 1000 } }); const p = await ctx.newPage(); const errors = [];
  p.on('pageerror', (e) => errors.push('pageerror: ' + e.message)); p.on('console', (m) => m.type() === 'error' && !/Failed to load resource/.test(m.text()) && errors.push(m.text()));
  const email = `ui${Date.now()}@example.com`;
  // sign up through the real form
  await p.goto(B + '/login'); await p.waitForSelector('#email');
  const r = await ctx.request.post(B + '/api/auth/signup', { data: { email, password: 'password123', name: 'Una Tester', plan: 'pro' } }); ok(r.ok(), 'signup');
  await p.goto(B + '/app#competitors'); await p.waitForSelector('#addform');
  ok(await p.locator('button.nav[data-k=products]').count() === 1, 'Products is in the sidebar');
  ok(await p.locator('button.nav[data-k=store]').count() === 1, 'Our store is in the sidebar (this backend has /api/store)');
  ok(await p.locator('button.nav[data-k=demo]').count() === 0, 'Live demo is hidden');
  // add through the UI with settings
  await p.fill('#cn', 'Lumen'); await p.fill('#cu', SHOP + '/');
  await p.click('#addform summary'); ok(await p.locator('#addform select').count() >= 3, 'add form: crawl settings open');
  await p.fill('#addform input[placeholder="200"]', '4'); await p.selectOption('#addform select >> nth=0', 'price_desc');
  await p.click('#addform button.primary'); await p.waitForSelector('.ccard');
  const sawCrawl = await p.waitForSelector('.ccard .crawling', { timeout: 6000 }).then(() => true, () => false);
  console.log('info  saw the crawling indicator on the card:', sawCrawl);
  await p.waitForFunction(() => !document.querySelector('.ccard .crawling') && /collected/.test(document.querySelector('.ccard')?.textContent || ''), null, { timeout: 60000 });
  let card = await p.locator('.ccard').first().innerText();
  ok(/4 collected/.test(card) && /of ~6/.test(card), 'card shows "4 collected of ~6": ' + (card.match(/Catalog\s*(.*)/)?.[1] || ''));
  ok(/shopify/i.test(card), 'card shows the platform');
  ok(await p.locator('.ccard button:has-text("Settings")').count() === 1, 'Settings button on the card');
  await p.screenshot({ path: S + '/n-card.png' });
  // settings dialog
  await p.click('.ccard button:has-text("Settings")'); await p.waitForSelector('dialog.modal[open]');
  await p.waitForSelector('dialog .checks label', { timeout: 10000 }); ok(await p.locator('dialog .checks label').count() >= 2, 'settings: menu categories listed as a checklist');
  ok(await p.inputValue('dialog input[placeholder="200"]') === '4', 'settings: product limit prefilled');
  await p.screenshot({ path: S + '/n-settings.png' });
  await p.fill('dialog input[placeholder="200"]', '50'); await p.click('dialog summary'); await p.fill('dialog details input[min="0.2"]', '0.2');
  await p.click('dialog button:has-text("Save")'); await p.waitForSelector('dialog.modal[open]', { state: 'detached', timeout: 10000 });
  const st = await (await ctx.request.get(B + '/api/state')).json(); ok(st.competitors[0].maxProducts === 50 && st.competitors[0].crawlSettings.delay_sec === 0.2, 'settings saved to the backend');
  await p.click('#sCrawl'); await p.waitForFunction(() => /6 collected/.test(document.querySelector('.ccard')?.textContent || ''), null, { timeout: 60000 }); ok(true, 'Crawl now collects all 6');
  // products page
  await p.click('button.nav[data-k=products]'); await p.waitForSelector('#presults tbody tr');
  ok(await p.locator('#presults tbody tr').count() === 6, 'products: 6 rows');
  const cats = await p.locator('select[data-k=category] option').allInnerTexts(); ok(cats.includes('Rings') && cats.includes('Earrings'), 'products: category dropdown from facets: ' + cats.join('/'));
  await p.selectOption('select[data-k=category]', 'Earrings'); await p.waitForFunction(() => document.querySelectorAll('#presults tbody tr').length === 2); ok(true, 'category filter -> 2 rows');
  await p.click('button:has-text("Clear filters")'); await p.waitForFunction(() => document.querySelectorAll('#presults tbody tr').length === 6);
  await p.check('input[data-k=on_sale]'); await p.waitForFunction(() => document.querySelectorAll('#presults tbody tr').length === 1);
  ok(/-20%/.test(await p.locator('#presults tbody').innerText()), 'on sale: discount shown'); await p.uncheck('input[data-k=on_sale]'); await p.waitForFunction(() => document.querySelectorAll('#presults tbody tr').length === 6);
  await p.fill('#pq', 'pendant'); await p.waitForFunction(() => document.querySelectorAll('#presults tbody tr').length === 1); ok(await p.inputValue('#pq') === 'pendant', 'search works and keeps the typed text');
  await p.fill('#pq', ''); await p.waitForFunction(() => document.querySelectorAll('#presults tbody tr').length === 6);
  await p.selectOption('select[data-k=sort]', 'price_asc'); await sleep(600); ok(/£80/.test(await p.locator('#presults tbody tr').first().innerText()), 'sort price low to high');
  await p.screenshot({ path: S + '/n-products.png' });
  // changes with new types
  await fetch(SHOP + '/__edit?action=drop'); await fetch(SHOP + '/__edit?action=sale'); await fetch(SHOP + '/__edit?action=oos'); await fetch(SHOP + '/__edit?action=add');
  await p.click('button.nav[data-k=competitors]'); await p.click('#sCrawl');
  const settled = async (pred) => { for (let i = 0; i < 120; i++) { const s = await (await ctx.request.get(B + '/api/state')).json(); if (!s.running && pred(s)) return s; await sleep(500); } return null; };
  ok(!!(await settled((s) => s.changes.some((c) => c.type === 'out_of_stock'))), 'second crawl finished with the catalogue edits');
  await p.reload(); await p.waitForSelector('.kpi, .ccard');
  await p.click('button.nav[data-k=changes]'); await p.waitForSelector('ul.plain li');
  const tags = await p.locator('.tag').allInnerTexts(); ok(['on sale', 'out of stock', 'product added'].every((t) => tags.includes(t)), 'changes show the new types: ' + [...new Set(tags)].join(', '));
  await p.screenshot({ path: S + '/n-changes.png' });
  // overview of one competitor
  await p.click('button.nav[data-k=overview]'); await p.waitForSelector('.kpi'); await p.click('.cf button:has-text("Lumen"), button:has-text("Lumen")');
  await sleep(800); const ov = await p.evaluate(() => document.body.innerText); ok(/Latest products & prices/i.test(ov), 'single-competitor overview uses product wording');
  await p.screenshot({ path: S + '/n-overview.png' });
  // reports page renders with new types
  await p.click('button.nav[data-k=reports]'); await p.waitForSelector('.kpi'); ok(true, 'reports render');
  // dark mode on products
  await p.click('button.nav[data-k=products]'); await p.waitForSelector('#presults tbody tr'); await p.click('.themerow'); await sleep(200); await p.screenshot({ path: S + '/n-products-dark.png' });
  ok(errors.length === 0, 'no console / page errors ' + errors.join(' | '));
  await b.close(); console.log(bad ? `\n${bad} FAILED of ${n}` : `\nall ${n} passed`); process.exit(bad ? 1 : 0);
})();
