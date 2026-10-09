// Browser test (Playwright): the landing page: exact headline, WebGL aurora, plans and the annual toggle, the looping detection theater,
// the Ctrl+K command palette, theme toggle, pinned story, compare slider, plan -> 2 s loader -> /login, mobile layout and reduced motion.
import os from 'node:os';
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const B = process.env.NODE_URL || 'http://localhost:3000', S = process.env.SHOTS_DIR || os.tmpdir();
let bad = 0, n = 0; const ok = (c, m) => { n++; if (!c) { bad++; console.error('FAIL', m); } else console.log('ok  ', m); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const ARGS = ['--no-sandbox', '--use-gl=swiftshader', '--enable-webgl', '--ignore-gpu-blocklist', '--enable-unsafe-swiftshader']; // software WebGL when there is no GPU
const quiet = (e) => !/GL Driver|GroupMarker|swiftshader|ReadPixels/i.test(e);
(async () => {
  const b = await chromium.launch({ executablePath: process.env.CHROMIUM || undefined, args: ARGS });
  // ---------------------------------------------------------------- desktop
  let ctx = await b.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: 'dark' }); let p = await ctx.newPage(); let errors = [];
  p.on('pageerror', (e) => errors.push('pageerror: ' + e.message)); p.on('console', (m) => m.type() === 'error' && errors.push(m.text()));
  await p.goto(B + '/'); await p.waitForSelector('#plans .plan'); await sleep(1500);
  ok(await p.evaluate(() => document.getElementById('h1').textContent) === 'Know what your rivals changed before your customers do.', 'headline text is exactly unchanged');
  ok((await p.content()).includes('Know what your rivals changed <em>before your customers do.</em>') || (await p.evaluate(() => document.getElementById('h1').innerText.replace(/\s+/g, ' ').trim())) === 'Know what your rivals changed before your customers do.', 'headline renders as one sentence');
  ok(await p.locator('.themebtn').count() === 1, 'theme toggle in the nav');
  ok(await p.evaluate(() => !document.body.classList.contains('nogl')), 'WebGL aurora is running');
  ok(await p.locator('#plans .plan').count() === 3 && await p.locator('#plans .plan.popular').count() === 1, 'three plans, one popular');
  const monthly = await p.locator('#plans .price').allInnerTexts(); await p.click('#ba'); await sleep(900); const yearly = await p.locator('#plans .price').allInnerTexts();
  ok(monthly.join() !== yearly.join() && yearly.every((t, i) => parseInt(yearly[i].replace(/\D/g, '')) < parseInt(monthly[i].replace(/\D/g, ''))), `annual toggle lowers prices (${monthly.join(' ')} -> ${yearly.join(' ')})`);
  await p.click('#bm'); await sleep(800);
  // theater runs through scenarios
  await p.evaluate(() => scrollTo(0, 0)); await sleep(11000);
  const rows = await p.locator('#feed .chg').count(); ok(rows >= 2, `the detection theater keeps looping (${rows} detections so far)`);
  ok(/\d+ changes? detected/.test(await p.locator('#sum').innerText()), 'digest summary updates');
  ok((await p.locator('#acts li').count()) >= 2, 'suggested actions are typed in');
  // command palette
  await p.keyboard.press('Control+k'); await p.waitForSelector('dialog#cmdk[open]'); ok(await p.locator('#cl li').count() >= 8, 'Ctrl+K opens the command palette with commands');
  await p.keyboard.type('pricing'); await sleep(150); ok(await p.locator('#cl li').first().innerText().then((t) => /Pricing/.test(t)), 'palette filters as you type');
  await p.keyboard.press('Enter'); await sleep(1500); ok(!(await p.locator('dialog#cmdk[open]').count()) && await p.evaluate(() => scrollY) > 1500, 'Enter jumps to the Pricing section');
  await p.keyboard.press('Control+k'); await p.waitForSelector('dialog#cmdk[open]'); await p.keyboard.press('Escape'); await sleep(200); ok(!(await p.locator('dialog#cmdk[open]').count()), 'Escape closes the palette');
  await p.evaluate(() => scrollTo(0, 0)); await sleep(300);
  // theme toggle
  const bg1 = await p.evaluate(() => getComputedStyle(document.body).backgroundColor); await p.click('nav .themebtn'); await sleep(300);
  ok(await p.evaluate(() => getComputedStyle(document.body).backgroundColor) !== bg1, 'theme toggle flips the page'); await p.click('nav .themebtn'); await sleep(200);
  // story steps
  const stepAt = async (f) => { await p.evaluate((f) => { const s = document.querySelector('.story'); scrollTo(0, s.offsetTop + (s.offsetHeight - innerHeight) * f); }, f); await sleep(900); return p.evaluate(() => ['pn1', 'pn2', 'pn3'].map((i) => document.getElementById(i).classList.contains('on') ? 1 : 0).join('')); };
  ok(await stepAt(.1) === '100' && await stepAt(.5) === '010' && await stepAt(.9) === '001', 'pinned story walks Crawl -> Detect -> Act while scrolling');
  // compare slider
  await p.evaluate(() => document.getElementById('cmp').scrollIntoView({ block: 'center' })); await sleep(4200);
  await p.fill('#cmpR', '20'); ok(await p.evaluate(() => document.getElementById('cmp').style.getPropertyValue('--x')) === '20%', 'compare slider follows the range input');
  ok(await p.locator('.cmp .hp').count() === 3, 'three change hotspots');
  await p.hover('.cmp .hp >> nth=0'); await sleep(300); ok(await p.locator('.cmp .hp >> nth=0').locator('span').evaluate((s) => getComputedStyle(s).opacity) === '1', 'hotspot tooltip shows on hover');
  // plan flow
  await p.evaluate(() => document.getElementById('pricing').scrollIntoView()); await sleep(800);
  await p.click('#plans .plan >> nth=1 >> button'); await sleep(300); ok(await p.locator('#loader.show').count() === 1 && /Pro/.test(await p.locator('#ltitle').innerText()), 'choosing a plan shows the loader');
  await p.waitForURL('**/login?plan=pro&billing=monthly', { timeout: 4000 }); ok(true, 'after the 2 s loader it lands on /login with the plan');
  ok(errors.filter(quiet).length === 0, 'no console / page errors on desktop ' + errors.filter(quiet).join(' | '));
  // signed-in nav
  await ctx.request.post(B + '/api/auth/signup', { data: { email: `land${Date.now()}@example.com`, password: 'password123' } }); await p.goto(B + '/'); await sleep(1200);
  ok(await p.locator('#signin').innerText() === 'Dashboard', 'signed-in visitors see Dashboard in the nav'); await ctx.close();
  // ---------------------------------------------------------------- mobile
  ctx = await b.newContext({ viewport: { width: 390, height: 844 }, colorScheme: 'light', hasTouch: true, isMobile: true }); p = await ctx.newPage(); errors = [];
  p.on('pageerror', (e) => errors.push(e.message)); await p.goto(B + '/'); await p.waitForSelector('#plans .plan'); await sleep(2500);
  ok(await p.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'mobile: no horizontal scroll (' + await p.evaluate(() => document.documentElement.scrollWidth) + 'px)');
  await p.screenshot({ path: S + '/L-m-hero.png' });
  for (const [k, y] of [['story', '#how'], ['diff', '#diff'], ['features', '#features'], ['pricing', '#pricing']]) { await p.evaluate((y) => { document.querySelector(y).scrollIntoView(); }, y); await sleep(1800); await p.screenshot({ path: `${S}/L-m-${k}.png` }); }
  ok(await p.evaluate(() => ['pn1', 'pn2', 'pn3'].every((i) => getComputedStyle(document.getElementById(i)).opacity === '1')), 'mobile: all three story panels are visible, stacked');
  ok(await p.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'mobile: still no horizontal scroll after scrolling the page');
  ok(errors.length === 0, 'no errors on mobile ' + errors.join('|')); await ctx.close();
  // ---------------------------------------------------------------- reduced motion
  ctx = await b.newContext({ viewport: { width: 1280, height: 800 }, reducedMotion: 'reduce' }); p = await ctx.newPage(); errors = []; p.on('pageerror', (e) => errors.push(e.message));
  await p.goto(B + '/'); await p.waitForSelector('#plans .plan'); await sleep(1200);
  ok(await p.evaluate(() => getComputedStyle(document.querySelector('#h1 em')).opacity) === '1' && await p.evaluate(() => getComputedStyle(document.querySelector('.stats')).opacity) === '1', 'reduced motion: content is visible immediately');
  ok(await p.locator('#feed .chg').count() === 3, 'reduced motion: the hero shows the finished detection story without looping');
  ok(await p.evaluate(() => ['pn1', 'pn2', 'pn3'].every((i) => getComputedStyle(document.getElementById(i)).opacity === '1')), 'reduced motion: story panels are shown, not pinned');
  ok(errors.length === 0, 'no errors with reduced motion ' + errors.join('|')); await ctx.close();
  await b.close(); console.log(bad ? `\n${bad} FAILED of ${n}` : `\nall ${n} passed`); process.exit(bad ? 1 : 0);
})();
