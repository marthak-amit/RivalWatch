// Shared helpers. Text is always inserted via textContent/createTextNode (competitor pages are untrusted).
const $ = (id) => document.getElementById(id);
const el = (tag, props = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === 'class') e.className = v; else if (k.startsWith('on')) e.addEventListener(k.slice(2), v); else if (v !== false && v != null) e.setAttribute(k, v === true ? '' : v);
  }
  e.append(...kids.flat().filter((x) => x != null && x !== false));
  return e;
};
async function api(path, { method = 'GET', body, noRedirect } = {}) {
  const res = await fetch(path, { method, credentials: 'same-origin', headers: body ? { 'content-type': 'application/json' } : method === 'GET' ? {} : { 'content-type': 'application/json' }, body: method === 'GET' ? undefined : JSON.stringify(body || {}) });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && !noRedirect) { location.href = '/login'; throw new Error('Not signed in'); }
  if (!res.ok) throw Object.assign(new Error(data.error || `Request failed (${res.status})`), { status: res.status });
  return data;
}
let toastTimer;
function toast(msg) {
  let t = $('toast'); if (!t) { t = el('div', { id: 'toast', role: 'status' }); document.body.append(t); }
  t.textContent = msg; t.classList.add('show'); clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('show'), 2600);
}
const money = (n) => '$' + n.toLocaleString();
const ago = (iso) => {
  if (!iso) return 'never';
  const s = (Date.now() - Date.parse(iso)) / 1000;
  return s < 60 ? 'just now' : s < 3600 ? Math.floor(s / 60) + 'm ago' : s < 86400 ? Math.floor(s / 3600) + 'h ago' : Math.floor(s / 86400) + 'd ago';
};
async function logout() { await api('/api/auth/logout', { method: 'POST', noRedirect: true }); location.href = '/'; }

// ---------------------------------------------------------------------------------------------------------------
// Dialogs and the price-change flow, shared by the dashboard and the admin panel.
// ---------------------------------------------------------------------------------------------------------------
/** A modal dialog. `body`/`foot` may be replaced later with dlg.setBody()/dlg.setFoot(). Esc, ✕ and a click outside close it. */
function modal(title, sub, body, foot) {
  const b = el('div', { class: 'mb' }, body), f = el('div', { class: 'mf' }, foot);
  const dlg = el('dialog', { class: 'modal', 'aria-label': title }, el('form', { method: 'dialog', novalidate: true, onsubmit: (e) => e.preventDefault() },
    el('div', { class: 'mh' }, el('div', {}, el('b', { style: 'font-size:16px' }, title), sub && el('div', { class: 'small mute' }, sub)), el('button', { class: 'iconbtn', type: 'button', 'aria-label': 'Close', onclick: () => dlg.close() }, '✕')), b, f));
  dlg.setBody = (...n) => b.replaceChildren(...n.flat().filter(Boolean)); dlg.setFoot = (...n) => { f.replaceChildren(...n.flat().filter(Boolean)); f.hidden = !f.children.length; };
  dlg.addEventListener('close', () => dlg.remove()); dlg.addEventListener('click', (e) => { if (e.target === dlg) dlg.close(); });
  document.body.append(dlg); dlg.showModal(); return dlg;
}
/** Format an amount in a currency code ("USD" -> $1,450.00); falls back to the number when the code is unknown. */
const cur = (n, c) => { if (n == null) return '–'; try { return new Intl.NumberFormat('en', { style: 'currency', currency: c || 'USD' }).format(n); } catch { return String(n); } };
/** How a competitor's price compares with ours. gap < 0 means the competitor is cheaper (the backend's convention). */
const gapText = (g) => (g == null ? 'not comparable' : g === 0 ? 'same price' : g < 0 ? `${Math.abs(g)}% cheaper than us` : `${g}% pricier than us`);
const gapClass = (g) => (g == null || g === 0 ? 'mute' : g < 0 ? 'hi' : 'good-t');
const PRICE_STATUS_PILL = { pending: 'pill acc', applied: 'pill good', failed: 'pill bad', cancelled: 'pill' };
const priceStatus = (s) => el('span', { class: PRICE_STATUS_PILL[s] || 'pill' }, s);

/** The "change a price" dialog. ctx = { request: POST path, action: (id, 'apply'|'cancel'|'revert') => path, onDone: () => void }.
 *  item = { sku, title, currency, price } (price is only a hint; the preview reads Magento's live price). Nothing changes in
 *  Magento until the person presses Apply, and Magento is written first: if it refuses, nothing changed anywhere. */
function priceDialog(ctx, item) {
  const st = { change: null, preview: null, err: '', busy: false, note: '' };
  const input = el('input', { type: 'number', step: '0.01', min: '0.01', inputmode: 'decimal', value: item.price ?? '', 'aria-label': 'New regular price', id: 'pcNew' });
  const dlg = modal('Change price', `${item.title || item.sku} · SKU ${item.sku}`, '', []);
  const call = async (fn) => { st.busy = true; st.err = ''; paint(); try { await fn(); } catch (e) { st.err = e.message; } st.busy = false; paint(); };
  const preview = () => call(async () => { const v = +input.value; if (!(v > 0)) throw new Error('Enter the new regular price'); const r = await api(ctx.request, { method: 'POST', body: { sku: item.sku, newPrice: v } }); st.change = r.change; st.preview = r.preview; st.note = ''; });
  const act = (a, after) => call(async () => { const r = await api(ctx.action(st.change.id, a), { method: 'POST' }); if (a === 'revert') { st.change = r.change; st.preview = r.preview; st.note = r.change.status === 'applied' ? 'Reverted: the old price is back in Magento.' : 'The revert is waiting for your agency to apply it.'; }
    else { st.change = r.change; st.note = a === 'apply' ? 'Done. Magento confirmed the new price.' : 'Cancelled. Nothing changed in Magento.'; if (a === 'cancel') st.preview = null; } ctx.onDone?.(); after?.(); });
  function paint() {
    const c = st.change, p = st.preview, money = (n) => cur(n, c?.currency || item.currency);
    const err = st.err && el('p', { class: 'err', role: 'alert' }, st.err);
    if (!c) { // step 1: ask for the new price
      dlg.setBody(el('p', { class: 'small mute' }, 'Set the new regular price. You will see the live price, the margin and where it puts you against competitors before anything changes.'),
        el('div', {}, el('label', { for: 'pcNew' }, `New regular price${item.currency ? ' (' + item.currency + ')' : ''}`), input), err);
      dlg.setFoot(el('button', { class: 'btn', type: 'button', onclick: () => dlg.close() }, 'Close'), el('button', { class: 'btn primary', type: 'button', disabled: st.busy, onclick: preview }, st.busy ? 'Checking…' : 'Preview'));
      return;
    }
    const row = (k, ...v) => el('div', { class: 'kv' }, el('span', { class: 'k' }, k), el('span', { class: 'v2' }, ...v));
    const blocks = [];
    if (st.note) blocks.push(el('p', { class: c.status === 'failed' ? 'err' : 'ok-note', role: 'status' }, st.note));
    blocks.push(el('div', { class: 'row between wrapx' }, el('div', {}, el('b', { style: 'font-size:22px' }, `${money(c.oldPrice)} → ${money(c.newPrice)}`), el('span', { class: 'mute', style: 'margin-left:10px;font-weight:700' }, `${c.changePct > 0 ? '+' : ''}${c.changePct}%`)), priceStatus(c.status)));
    if (p) {
      blocks.push(el('div', {}, row('Current price', money(p.currentPrice), el('span', { class: 'pill' + (p.priceSource === 'magento' ? ' good' : '') }, p.priceSource === 'magento' ? 'live from Magento' : 'last synced value')),
        p.cost != null ? row('Margin', `${p.marginBefore}% → `, el('b', {}, `${p.marginAfter}%`), el('span', { class: 'mute small' }, ` (cost ${money(p.cost)})`)) : row('Margin', el('span', { class: 'mute' }, 'Magento has no cost for this product')),
        p.salePrice != null && row('Shoppers pay', `${money(p.shownBefore)} → ${money(p.shownAfter)}`, el('span', { class: 'mute small' }, `a sale price of ${money(p.salePrice)} stays`))));
      if (p.warnings?.length) blocks.push(el('ul', { class: 'warns' }, p.warnings.map((w) => el('li', {}, w))));
      const pos = p.position;
      if (pos && (pos.competitors?.length || pos.sameProduct?.length || pos.notes?.length)) {
        blocks.push(el('div', {}, el('div', { class: 'h-sec' }, `Where this puts us${pos.type ? ' · ' + pos.type : ''}`),
          ...(pos.competitors || []).map((x) => row(x.name, `median ${money(x.median)}`, el('span', { class: gapClass(x.gapAfter) }, ` ${gapText(x.gapBefore)} → ${gapText(x.gapAfter)}`))),
          ...(pos.sameProduct || []).map((x) => row(x.name, el('a', { href: x.url, target: '_blank', rel: 'noopener noreferrer' }, 'same product'), ` ${money(x.price)} `, el('span', { class: gapClass(x.gapAfter) }, `${gapText(x.gapBefore)} → ${gapText(x.gapAfter)}`))),
          ...(pos.notes || []).map((n) => el('p', { class: 'small mute', style: 'margin-top:6px' }, n))));
      }
    }
    if (c.error) blocks.push(el('p', { class: 'err' }, c.error));
    if (c.appliedBy || c.status === 'pending') blocks.push(el('p', { class: 'small mute' }, c.status === 'pending' ? `Requested by ${c.requestedBy}. Nothing has changed in Magento yet.` : `Requested by ${c.requestedBy}${c.appliedBy ? `, applied by ${c.appliedBy}` : ''}.`));
    dlg.setBody(blocks, err);
    const close = el('button', { class: 'btn', type: 'button', onclick: () => dlg.close() }, 'Close');
    if (c.status === 'pending') {
      const can = p ? p.canApply : true;
      dlg.setFoot(el('button', { class: 'btn danger', type: 'button', disabled: st.busy, onclick: () => act('cancel') }, 'Cancel this change'), el('span', { style: 'flex:1' }), close,
        el('button', { class: 'btn primary', type: 'button', disabled: st.busy || !can, title: can ? '' : 'This price can’t be applied yet. See the warnings.', onclick: () => act('apply') }, st.busy ? 'Working…' : 'Apply to Magento'));
    } else if (c.status === 'applied') dlg.setFoot(el('button', { class: 'btn', type: 'button', disabled: st.busy, onclick: () => act('revert') }, 'Revert to ' + money(c.oldPrice)), close);
    else dlg.setFoot(el('button', { class: 'btn', type: 'button', onclick: () => { st.change = null; st.preview = null; st.err = ''; st.note = ''; paint(); } }, 'Try another price'), close);
  }
  paint(); setTimeout(() => input.focus(), 50); return dlg;
}
