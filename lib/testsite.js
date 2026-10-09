// Three fake competitor sites the team controls. Editing them live is how the
// demo proves change detection end to end.
export const seed = () => ({
  acme: {
    name: 'Acme CRM', tagline: 'CRM for small teams', promo: null, extra: [],
    description: 'Acme CRM helps small sales teams track every deal, contact and follow-up in one place.',
    highlights: ['Pipeline you can read at a glance', 'Automatic follow-up reminders', 'Built for teams under 50'],
    email: 'hello@acme-crm.example', socials: ['https://x.com/acmecrm', 'https://www.linkedin.com/company/acmecrm'],
    products: [{ name: 'CRM Lite', price: 29 }, { name: 'CRM Growth', price: 79 }, { name: 'CRM Enterprise', price: 199 }],
  },
  globex: {
    name: 'Globex Analytics', tagline: 'Dashboards without the data team', promo: 'Free setup for annual plans',
    extra: [],
    description: 'Globex Analytics turns raw product data into dashboards anyone can use, with no data team required.',
    highlights: ['Dashboards in minutes', 'Connects to 40+ data sources', 'Share live reports with one link'],
    email: 'sales@globex-analytics.example', socials: ['https://x.com/globexhq', 'https://www.youtube.com/@globexanalytics', 'https://github.com/globex-analytics'],
    products: [{ name: 'Insights Solo', price: 19 }, { name: 'Insights Team', price: 59 }],
  },
  initech: {
    name: 'Initech Helpdesk', tagline: 'Support tickets, simplified', promo: null, extra: [],
    description: 'Initech Helpdesk keeps support tickets, customers and answers together in a single shared inbox.',
    highlights: ['One shared inbox for the whole team', 'Canned answers and macros', 'SLA timers that nudge the team'],
    email: 'support@initech-helpdesk.example', socials: ['https://www.linkedin.com/company/initech-helpdesk', 'https://www.facebook.com/initechhelpdesk'],
    products: [{ name: 'Helpdesk Basic', price: 15 }, { name: 'Helpdesk Plus', price: 45 }, { name: 'Helpdesk Scale', price: 120 }],
  },
});

/** Reset a user's sites in place so existing references stay valid. */
export const reset = (sites) => { for (const k of Object.keys(sites)) delete sites[k]; Object.assign(sites, seed()); };

const footer = (s) => `<footer>Contact <a href="mailto:${s.email}">${s.email}</a> ${s.socials.map((u) => `<a href="${u}">${new URL(u).hostname.replace(/^www\./, '')}</a>`).join(' ')}</footer>`;
const page = (s, body, title = 'Home', desc = s.description) => `<!doctype html><html><head><title>${title} | ${s.name}</title><meta name="description" content="${desc}"></head><body>${body}${footer(s)}</body></html>`;
const nav = (base, s) => `<nav><a href="${base}/">Home</a> <a href="${base}/pricing">Pricing</a> <a href="${base}/blog">Blog</a>${
  s.extra.map((p) => ` <a href="${base}/${p.slug}">${p.title}</a>`).join('')}</nav>`;

/** `base` is the URL prefix this site is served under, e.g. /test/<uid>/acme */
export function render(sites, id, rest, base) {
  const s = sites[id];
  if (!s) return null;
  const n = nav(base, s);
  if (rest === '') return page(s, `${n}${s.promo ? `<blockquote>${s.promo}</blockquote>` : ''}<h1>${s.tagline}</h1><p>${s.description}</p>${s.highlights.map((h) => `<h2>${h}</h2>`).join('')}`);
  if (rest === 'pricing') return page(s, `${n}<h1>Pricing</h1><ul>${s.products.map((p) => `<li><strong>${p.name}</strong> — $${p.price}/mo</li>`).join('')}</ul>`, 'Pricing');
  if (rest === 'blog') return page(s, `${n}<h1>Blog</h1><p>Company news and tips.</p>`, 'Blog');
  const extra = s.extra.find((p) => p.slug === rest);
  if (extra) return page(s, `${n}<h1>${extra.title}</h1><p>${extra.body}</p>`, extra.title);
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
