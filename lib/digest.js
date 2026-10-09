// AI step: changes -> digest with 3-5 suggested actions.
// Uses Claude when ANTHROPIC_API_KEY is set, otherwise a rule-based fallback
// so the demo works offline.
import { describe } from './diff.js';

const MODEL = process.env.DIGEST_MODEL || 'claude-sonnet-5-5';

export async function makeDigest(changes) {
  if (process.env.ANTHROPIC_API_KEY) {
    try { return { ...(await viaClaude(changes)), source: 'claude' }; }
    catch (e) { console.warn('Claude digest failed, using rules:', e.message); }
  }
  return { ...viaRules(changes), source: 'rules' };
}

async function viaClaude(changes) {
  const lines = changes.map((c) => `- [${c.competitor}] ${describe(c)}`).join('\n');
  const res = await fetch('https://api.anthropic.com/v1/messages', {
    method: 'POST',
    headers: { 'x-api-key': process.env.ANTHROPIC_API_KEY, 'anthropic-version': '2023-06-01', 'content-type': 'application/json' },
    body: JSON.stringify({
      model: MODEL, max_tokens: 1200,
      messages: [{ role: 'user', content:
        `You are a competitive-intelligence analyst. These changes were detected on competitor websites:\n${lines}\n\n` +
        `Reply with JSON only: {"summary": "2-3 sentence digest", "actions": [{"title": "...", "why": "...", "priority": "high|medium|low"}]}. ` +
        `Give between 3 and 5 concrete actions for our team.` }],
    }),
    signal: AbortSignal.timeout(30_000),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const text = (await res.json()).content?.[0]?.text ?? '';
  const out = JSON.parse(text.slice(text.indexOf('{'), text.lastIndexOf('}') + 1));
  if (!out.summary || !Array.isArray(out.actions) || out.actions.length < 1) throw new Error('bad shape');
  return { summary: out.summary, actions: out.actions.slice(0, 5) };
}

function viaRules(changes) {
  const actions = [];
  const add = (a) => { if (actions.length < 5) actions.push(a); };
  const by = (t) => changes.filter((c) => c.type === t);
  for (const c of by('price_change').filter((c) => c.pct < 0)) add({ priority: 'high', title: `Review our pricing against ${c.competitor}'s ${c.name} cut`, why: `${c.competitor} dropped ${c.name} ${c.from} → ${c.to} (${c.pct}%). Decide whether to match, bundle, or differentiate on value.` });
  for (const c of by('price_change').filter((c) => c.pct > 0)) add({ priority: 'medium', title: `Use ${c.competitor}'s price rise in sales conversations`, why: `${c.name} went ${c.from} → ${c.to} (+${c.pct}%). Target their price-sensitive customers with a switching offer.` });
  for (const c of by('new_promo')) add({ priority: 'high', title: `Counter ${c.competitor}'s promotion`, why: `They launched “${c.text}”. Consider a time-boxed offer or highlight non-price advantages.` });
  for (const c of by('product_added')) add({ priority: 'medium', title: `Assess ${c.competitor}'s new "${c.name}" tier`, why: `Added at ${c.price}. Check feature overlap and whether our packaging has a gap.` });
  for (const c of by('new_page')) add({ priority: 'low', title: `Read ${c.competitor}'s new page ${c.path}`, why: 'New pages often signal launches or repositioning; review messaging and update battlecards.' });
  for (const c of by('product_removed')) add({ priority: 'medium', title: `Court ${c.competitor}'s ${c.name} customers`, why: `${c.name} was removed from their site; customers may be looking for alternatives.` });
  const pad = ['Brief sales and support on these changes this week', 'Update the competitor battlecards', 'Keep monitoring these competitors daily'];
  for (const t of pad) if (actions.length < 3) add({ priority: 'low', title: t, why: 'Keeps the team aligned on competitor movements.' });
  const names = [...new Set(changes.map((c) => c.competitor))].join(', ');
  const n = (k, one, many) => `${by(k).length} ${by(k).length === 1 ? one : many}`;
  return { summary: `${changes.length} change${changes.length === 1 ? '' : 's'} detected across ${names}: ${n('price_change', 'price move', 'price moves')}, ${n('new_promo', 'new promotion', 'new promotions')}, ${n('new_page', 'new page', 'new pages')}.`, actions };
}
