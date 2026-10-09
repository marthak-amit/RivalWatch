import test from 'node:test';
import assert from 'node:assert/strict';
import { extract } from '../lib/extract.js';
import { diff } from '../lib/diff.js';
import { htmlToMarkdown } from '../lib/markdown.js';
import { makeDigest } from '../lib/digest.js';

const base = 'http://x.test/a/';
const md = (html) => htmlToMarkdown(html, base).markdown;

test('extract finds products, promos and pages', () => {
  const e = extract(md('<nav><a href="/a/pricing">P</a><a href="https://other.com/z">ext</a></nav><blockquote>30% off annual plans</blockquote><ul><li><strong>Pro</strong> — $79/mo</li></ul>'), base);
  assert.equal(e.products.pro.amount, 79);
  assert.deepEqual(e.promotions, ['30% off annual plans']);
  assert.deepEqual(e.pages, ['/pricing']);
});

test('diff detects price, promo and page changes', () => {
  const a = { products: { pro: { name: 'Pro', price: '$100/mo', amount: 100 } }, promotions: [], pages: ['/'] };
  const b = { products: { pro: { name: 'Pro', price: '$80/mo', amount: 80 } }, promotions: ['Sale 10% off'], pages: ['/', '/new'] };
  const types = diff(a, b).map((c) => c.type).sort();
  assert.deepEqual(types, ['new_page', 'new_promo', 'price_change']);
  assert.equal(diff(a, b).find((c) => c.type === 'price_change').pct, -20);
});

test('rule-based digest gives 3-5 actions', async () => {
  delete process.env.ANTHROPIC_API_KEY;
  const d = await makeDigest([{ type: 'new_page', path: '/x', competitor: 'Acme' }]);
  assert.ok(d.actions.length >= 3 && d.actions.length <= 5);
});
