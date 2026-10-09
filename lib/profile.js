// Company profile extracted from a competitor's crawled pages: who they are,
// how they position themselves, and how to reach / follow them.
const SOCIAL = { 'x.com': 'X', 'twitter.com': 'X', 'linkedin.com': 'LinkedIn', 'facebook.com': 'Facebook', 'instagram.com': 'Instagram',
  'youtube.com': 'YouTube', 'github.com': 'GitHub', 'tiktok.com': 'TikTok' };
const EMAIL = /[\w.+-]+@[\w-]+(?:\.[\w-]+)+/g;
const clip = (s, n) => (s.length > n ? s.slice(0, n - 1).trimEnd() + '…' : s);
const plain = (s) => s.replace(/\[([^\]]+)\]\([^)]*\)/g, '$1').replace(/[*_`>]/g, '').replace(/\s+/g, ' ').trim();

/** @param {{url:string,title?:string,description?:string,markdown:string,links:string[]}[]} pages first page is the entry/home page */
export function buildProfile(pages, rootPath = '') {
  const home = pages[0];
  const heads = [...home.markdown.matchAll(/^#{1,3}\s+(.+)$/gm)].map((m) => plain(m[1])).filter(Boolean);
  const firstPara = home.markdown.split('\n').map((l) => l.trim()).find((l) => l.length > 40 && !/^[#>\-[|]/.test(l));
  const description = home.description || (firstPara ? plain(firstPara) : '');

  const socials = [], seen = new Set();
  for (const p of pages) for (const l of p.links) {
    let u; try { u = new URL(l); } catch { continue; }
    const host = u.hostname.replace(/^www\./, '');
    const net = SOCIAL[host] ?? Object.entries(SOCIAL).find(([k]) => host.endsWith('.' + k))?.[1];
    if (net && !seen.has(net) && u.pathname.length > 1) { seen.add(net); socials.push({ network: net, url: u.href }); }
  }
  const text = pages.map((p) => p.markdown).join('\n');
  const emails = [...new Set([...text.matchAll(/mailto:([^\s)?]+)/g)].map((m) => m[1]).concat(text.match(EMAIL) ?? []))]
    .map((e) => e.toLowerCase()).filter((e) => EMAIL.test(e) && !/\.(png|jpe?g|gif|svg|webp)$/.test(e)).slice(0, 2);
  EMAIL.lastIndex = 0;

  const rel = (u) => { const p = new URL(u).pathname.replace(/\/$/, ''); return (p.startsWith(rootPath) ? p.slice(rootPath.length) : p) || '/'; };
  const keyPages = pages.map((p) => ({ path: rel(p.url), title: clip((p.title || '').split('|')[0].trim() || rel(p.url), 40) }));

  return {
    headline: clip(heads[0] ?? home.title ?? '', 120), description: clip(description, 280),
    positioning: heads.slice(1, 5).map((h) => clip(h, 70)), socials: socials.slice(0, 6), emails, keyPages: keyPages.slice(0, 8),
  };
}
