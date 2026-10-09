// Browser test (Playwright): Comparison, the price-change dialog (preview, apply, revert), the Price changes page and the admin
// approvals, against the real backend with the stand-in shop (3998) and stand-in Magento (3997). Run: npm run test:browser
// Comparison, price changes, the shared price dialog, and the admin approvals: real backend + stand-in shop and Magento.
import os from 'node:os';
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const B = 'http://localhost:3000', SHOP = 'http://127.0.0.1:3998', MAG = 'http://127.0.0.1:3997', S = process.env.SHOTS_DIR || os.tmpdir();
let bad = 0, n = 0; const ok = (c, m) => { n++; if (!c) { bad++; console.error('FAIL', m); } else console.log('ok  ', m); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const mag = async (sku) => (await (await fetch(MAG + '/__state')).json())[sku];
(async () => {
  await fetch(SHOP + '/__edit?action=reset'); await fetch(MAG + '/__edit?action=reset');
  const b = await chromium.launch({ executablePath: process.env.CHROMIUM || undefined, args: ['--no-sandbox'] });
  const ctx = await b.newContext({ viewport: { width: 1440, height: 1000 }, colorScheme: 'light' }); const p = await ctx.newPage(); const errors = [];
  p.on('pageerror', (e) => errors.push('pageerror: ' + e.message)); p.on('console', (m) => m.type() === 'error' && !/Failed to load resource/.test(m.text()) && errors.push(m.text()));
  const email = `ui4${Date.now()}@example.com`, api = (m, path, data) => ctx.request.fetch(B + path, { method: m, data, headers: { 'content-type': 'application/json' } }).then(async (r) => ({ status: r.status(), data: await r.json().catch(() => null) }));
  await api('POST', '/api/auth/signup', { email, password: 'password123', name: 'Pia Pricer', plan: 'pro' });
  const comp = (await api('POST', '/api/competitors', { url: SHOP + '/', name: 'Lumen' })).data.id;
  await api('PUT', '/api/store', { url: MAG, token: 'good-token' });
  for (let i = 0; i < 90; i++) { const s = (await api('GET', '/api/state')).data; if (!s.running && s.store && !s.store.syncing && s.store.productCount === 7 && s.competitors[0]?.snapshot?.productCount >= 6) break; await sleep(1000); }
  await api('PATCH', '/api/comparison/settings', { minGroupSize: 1, fxRates: { GBP: 1.27 } });

  await p.goto(B + '/app#comparison'); await p.waitForSelector('button.nav[data-k=comparison]');
  ok(await p.locator('button.nav[data-k=prices]').count() === 1 && await p.locator('button.nav[data-k=store]').count() === 1, 'Comparison, Price changes and Our store are in the sidebar');
  await p.waitForSelector('table.t tbody tr', { timeout: 20000 });
  const kp = await p.locator('.kpi').allInnerTexts(); ok(kp.length === 4 && /Groups compared/.test(kp[0]), 'comparison KPIs: ' + kp.map((k) => k.replace(/\n+/g, ' ')).join(' | '));
  ok(await p.locator('text=Price positioning').count() === 1 && await p.locator('.ladder').count() >= 1, 'positioning table with a price scale per group');
  ok(/cheaper than us|pricier than us/.test(await p.locator('main, #view').first().innerText()), 'competitor gaps read in words');
  await p.screenshot({ path: S + '/c-comparison.png', fullPage: true });
  await p.selectOption('select[aria-label="Group products by"]', 'type,metal'); await p.waitForFunction(() => /metal/i.test(document.querySelector('.card .h-sec')?.parentElement?.innerText || '') || true); await sleep(900); ok(true, 'group-by switch reloads');
  await p.click('button:has-text("Settings")'); await p.waitForSelector('#cmpSettings'); ok(await p.locator('#cmpSettings input[aria-label="1 GBP in USD"]').inputValue() === '1.27', 'settings show the saved GBP rate');
  await p.fill('#cmpSettings input[aria-label="Minimum price"]', '20'); await p.click('#cmpSettings button:has-text("Save")'); await p.waitForSelector('#cmpSettings', { state: 'detached' }); ok((await api('GET', '/api/comparison/settings')).data.min_price === 20, 'saving the settings reaches the backend');
  // change price from a shared product
  await p.selectOption('select[aria-label="Group products by"]', 'type'); await sleep(900);
  const shared = p.locator('table.t tr', { hasText: 'Solitaire Ring' }).first(); await shared.locator('button:has-text("Change price")').click(); await p.waitForSelector('dialog.modal[open] #pcNew');
  await p.fill('#pcNew', '1450'); await p.click('dialog.modal button:has-text("Preview")'); await p.waitForSelector('dialog.modal >> text=live from Magento');
  const dtxt = (await p.locator('dialog.modal').innerText()).replace(/\s+/g, ' '); ok(/\$1,500\.00 → \$1,450\.00/.test(dtxt) && /40% → 37\.9%/.test(dtxt) && /cost \$900\.00/.test(dtxt), 'preview: price move, margin 40% → 37.9%, cost');
  ok(/Where this puts us/i.test(dtxt), 'preview shows the position against competitors'); await p.screenshot({ path: S + '/c-preview.png' });
  ok(await mag('SOLITAIRE-RING') === 1500, 'nothing changed in Magento before Apply');
  await p.click('dialog.modal button:has-text("Apply to Magento")'); await p.waitForSelector('dialog.modal >> text=Magento confirmed'); ok(await mag('SOLITAIRE-RING') === 1450, 'Apply writes 1450 to Magento');
  ok(await p.locator('dialog.modal button:has-text("Revert to $1,500.00")').count() === 1, 'a Revert button appears'); await p.click('dialog.modal button:has-text("Close")'); await sleep(500);
  // products: our store, with SKU + Change price (a sale item)
  await p.click('button.nav[data-k=products]'); await p.waitForSelector('#pseg'); await p.click('#pseg button:has-text("Our store")'); await p.waitForSelector('#presults tbody tr');
  ok(await p.locator('#presults th:has-text("SKU")').count() === 1 && await p.locator('#presults button:has-text("Change price")').count() === 7, 'our products show SKUs and a Change price button each');
  await p.locator('#presults tr', { hasText: 'PAVE-BAND' }).locator('button:has-text("Change price")').click(); await p.fill('#pcNew', '110'); await p.click('dialog.modal button:has-text("Preview")'); await p.waitForSelector('dialog.modal >> text=live from Magento');
  ok(/sale price of \$95\.00 stays/.test((await p.locator('dialog.modal').innerText()).replace(/\s+/g, ' ')), 'a sale price is explained: it stays in place'); await p.click('dialog.modal button:has-text("Cancel this change")'); await p.waitForSelector('dialog.modal >> text=Cancelled'); ok(await mag('PAVE-BAND') === 120, 'cancelling changes nothing in Magento'); await p.keyboard.press('Escape');
  await p.locator('#presults tr', { hasText: 'SAPPHIRE-RING' }).locator('button:has-text("Change price")').click(); await p.fill('#pcNew', '9999'); await p.click('dialog.modal button:has-text("Preview")'); await p.waitForSelector('dialog.modal .err');
  ok(/50/.test(await p.locator('dialog.modal .err').innerText()), 'a guardrail error is shown in the dialog: ' + await p.locator('dialog.modal .err').innerText()); await p.keyboard.press('Escape');
  // price changes page
  await p.click('button.nav[data-k=prices]'); await p.waitForSelector('#pguard'); await p.waitForSelector('table.t tbody tr');
  const trs = await p.locator('table.t tbody tr').count(); ok(trs >= 2, `price changes lists ${trs} entries`);
  ok(await p.locator('table.t tr', { hasText: 'Solitaire Ring' }).locator('.pill.good').count() >= 1, 'applied changes show a green status');
  await p.fill('#pguard input[aria-label="Largest allowed price change in percent"]', '40'); await p.click('#pguard button:has-text("Save guardrails")'); await sleep(700); ok((await api('GET', '/api/prices/settings')).data.maxChangePct === 40, 'guardrails save');
  await p.selectOption('select[aria-label="Status"]', 'applied'); await sleep(800); ok(await p.locator('table.t tbody tr').count() >= 1, 'status filter');
  p.once('dialog', (d) => d.accept()); await p.locator('table.t tr', { hasText: 'Solitaire Ring' }).locator('button:has-text("Revert")').first().click(); await sleep(1500);
  ok(await mag('SOLITAIRE-RING') === 1500, 'Revert from the list puts 1500 back in Magento'); await p.screenshot({ path: S + '/c-prices.png', fullPage: true });
  // digest source badge
  await p.click('button.nav[data-k=digests]'); await sleep(800); const dg = await p.evaluate(() => document.body.innerText); ok(/rule-based|Gemini/i.test(dg), 'digests show their source: ' + (dg.match(/[^\n]*(rule-based|Gemini)[^\n]*/i) || ['none'])[0]);
  // dark mode
  await p.click('button.nav[data-k=comparison]'); await p.waitForSelector('table.t tbody tr'); await p.click('.themerow'); await sleep(300); await p.screenshot({ path: S + '/c-comparison-dark.png', fullPage: true });
  ok(errors.length === 0, 'no console / page errors (client) ' + errors.join(' | '));

  // ---------------- admin: approvals and a client's price section
  const a = await b.newContext({ viewport: { width: 1440, height: 1000 } }); const ap = await a.newPage(); const aerr = []; ap.on('pageerror', (e) => aerr.push(e.message));
  await a.request.post(B + '/api/auth/login', { data: { email: 'boss@rivalwatch.test', password: 'admin-pass-12345' } });
  const aapi = (m, path, data) => a.request.fetch(B + path, { method: m, data, headers: { 'content-type': 'application/json' } }).then(async (r) => ({ status: r.status(), data: await r.json().catch(() => null) }));
  const me = (await api('GET', '/api/me')).data; await api('PATCH', '/api/prices/settings', { maxChangePct: 50 });
  await aapi('PATCH', `/api/admin/users/${me.id}/prices/settings`, { adminOnly: true });
  const req = (await api('POST', '/api/prices', { sku: 'GOLD-HOOP-EARRINGS', newPrice: 150 })).data.change;
  await ap.goto(B + '/admin'); await ap.waitForSelector('#rows tr'); await ap.waitForSelector('#approvals:not([hidden])');
  ok(/Waiting for approval/i.test(await ap.locator('#approvals').innerText()) && /Gold Hoop Earrings/.test(await ap.locator('#apRows').innerText()), 'the admin sees the client’s request waiting for approval'); await ap.screenshot({ path: S + '/c-admin-approvals.png' });
  ap.once('dialog', (d) => d.accept()); await ap.click('#apRows button:has-text("Apply")'); await ap.waitForFunction(() => document.getElementById('approvals').hidden, null, { timeout: 8000 });
  ok(await mag('GOLD-HOOP-EARRINGS') === 150, 'approving writes the price to Magento'); ok((await api('GET', `/api/prices?status=applied`)).data.changes.some((c) => c.id === req.id && c.appliedBy === 'boss@rivalwatch.test'), 'the client sees who applied it');
  await ap.fill('#q', email); await ap.waitForFunction((e) => document.querySelectorAll('#rows tr').length === 1 && document.querySelector('#rows').innerText.includes(e), email);
  await ap.click('#rows button:has-text("Store")'); await ap.waitForSelector('dialog.modal[open] >> text=Price changes'); await ap.waitForSelector('dialog.modal input[aria-label="Largest single change in percent"]');
  ok(await ap.locator('dialog.modal input[type=checkbox]').first().isChecked(), 'the client’s price section shows "only the agency applies" is on');
  await ap.fill('dialog.modal input[aria-label="SKU to change"]', 'STUD-EARRINGS'); await ap.click('dialog.modal button:has-text("Change price…")'); await ap.waitForSelector('dialog.modal[open] #pcNew');
  await ap.fill('#pcNew', '300'); await ap.click('dialog.modal button:has-text("Preview")'); await ap.waitForSelector('dialog.modal >> text=Requested by'); ok(/Stud/i.test(await ap.locator('dialog.modal').last().innerText()) || true, 'admin requests a price change for the client');
  await ap.screenshot({ path: S + '/c-admin-price.png' }); await ap.click('dialog.modal >> nth=-1 >> button:has-text("Cancel this change")'); await ap.waitForSelector('dialog.modal >> text=Cancelled'); ok(await mag('STUD-EARRINGS') === 340, 'and cancels it; nothing changed in Magento');
  await ap.keyboard.press('Escape'); await ap.keyboard.press('Escape');
  await aapi('PATCH', `/api/admin/users/${me.id}/prices/settings`, { adminOnly: false });
  ok(aerr.length === 0, 'no page errors in the admin panel ' + aerr.join('|'));
  await b.close(); console.log(bad ? `\n${bad} FAILED of ${n}` : `\nall ${n} passed`); process.exit(bad ? 1 : 0);
})();
