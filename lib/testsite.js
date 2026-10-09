// Three fake competitor sites the team controls. Editing them live is how the
// demo proves change detection end to end.
export const seed = () => ({
  acme: {
    name: 'Acme CRM', tagline: 'CRM for small teams', promo: null, extra: [],
    products: [{ name: 'CRM Lite', price: 29 }, { name: 'CRM Growth', price: 79 }, { name: 'CRM Enterprise', price: 199 }],
  },
  globex: {
    name: 'Globex Analytics', tagline: 'Dashboards without the data team', promo: 'Free setup for annual plans',
    extra: [],
    products: [{ name: 'Insights Solo', price: 19 }, { name: 'Insights Team', price: 59 }],
  },
  initech: {
    name: 'Initech Helpdesk', tagline: 'Support tickets, simplified', promo: null, extra: [],
    products: [{ name: 'Helpdesk Basic', price: 15 }, { name: 'Helpdesk Plus', price: 45 }, { name: 'Helpdesk Scale', price: 120 }],
  },
});

/** Reset a user's sites in place so existing references stay valid. */
export const reset = (sites) => { for (const k of Object.keys(sites)) delete sites[k]; Object.assign(sites, seed()); };

const page = (s, body) => `<!doctype html><html><head><title>${s.name}</title></head><body>${body}</body></html>`;
const nav = (base, s) => `<nav><a href="${base}/">Home</a> <a href="${base}/pricing">Pricing</a> <a href="${base}/blog">Blog</a>${
  s.extra.map((p) => ` <a href="${base}/${p.slug}">${p.title}</a>`).join('')}</nav>`;

/** `base` is the URL prefix this site is served under, e.g. /test/<uid>/acme */
export function render(sites, id, rest, base) {
  const s = sites[id];
  if (!s) return null;
  const n = nav(base, s);
  if (rest === '') return page(s, `${n}${s.promo ? `<blockquote>${s.promo}</blockquote>` : ''}<h1>${s.name}</h1><p>${s.tagline}</p>`);
  if (rest === 'pricing') return page(s, `${n}<h1>Pricing</h1><ul>${s.products.map((p) => `<li><strong>${p.name}</strong> — $${p.price}/mo</li>`).join('')}</ul>`);
  if (rest === 'blog') return page(s, `${n}<h1>Blog</h1><p>Company news and tips.</p>`);
  const extra = s.extra.find((p) => p.slug === rest);
  if (extra) return page(s, `${n}<h1>${extra.title}</h1><p>${extra.body}</p>`);
  return null;
}

/** Apply a named edit. Returns a human-readable description of what changed. */
export function edit(sites, id, action) {
  const s = sites[id];
  if (!s) throw new Error('unknown site');
  switch (action) {
    case 'price_drop': { const p = s.products[0]; const old = p.price; p.price = Math.round(old * 0.8); return `${s.name}: ${p.name} $${old} → $${p.price}`; }
    case 'price_hike': { const p = s.products.at(-1); const old = p.price; p.price = Math.round(old * 1.15); return `${s.name}: ${p.name} $${old} → $${p.price}`; }
    case 'add_tier': s.products.push({ name: 'Enterprise AI', price: 499 }); return `${s.name}: added Enterprise AI tier`;
    case 'promo': s.promo = '30% off all annual plans — use code SPRING30 (limited time)'; return `${s.name}: launched 30% off promotion`;
    case 'new_page': if (!s.extra.some((p) => p.slug === 'ai-assistant')) s.extra.push({ slug: 'ai-assistant', title: 'AI Assistant', body: 'Meet our new AI assistant, launching this month.' }); return `${s.name}: published /ai-assistant`;
    default: throw new Error('unknown action');
  }
}
