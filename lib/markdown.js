// Minimal HTML -> markdown + link extraction. Good enough for competitor
// marketing/pricing pages; not a full readability engine.
const decode = (s) => s
  .replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"')
  .replace(/&#39;/g, "'").replace(/&mdash;/g, '—').replace(/&ndash;/g, '–').replace(/&nbsp;/g, ' ');

export function htmlToMarkdown(html, baseUrl) {
  const abs = (href) => { try { return new URL(href, baseUrl).href; } catch { return href; } };
  const title = decode((html.match(/<title[^>]*>([\s\S]*?)<\/title>/i)?.[1] ?? '').trim());
  const md = html
    .replace(/<(script|style|noscript|svg|head)[\s\S]*?<\/\1>/gi, '')
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/<h([1-4])[^>]*>([\s\S]*?)<\/h\1>/gi, (_, n, t) => `\n\n${'#'.repeat(n)} ${t}\n\n`)
    .replace(/<a\s[^>]*?href=["']([^"']*)["'][^>]*>([\s\S]*?)<\/a>/gi, (_, h, t) => `[${t.replace(/<[^>]+>/g, '').trim()}](${abs(h)})`)
    .replace(/<(strong|b)\b[^>]*>([\s\S]*?)<\/\1>/gi, '**$2**')
    .replace(/<li[^>]*>/gi, '\n- ')
    .replace(/<blockquote[^>]*>/gi, '\n\n> ')
    .replace(/<tr[^>]*>/gi, '\n').replace(/<\/t[dh]>/gi, ' | ')
    .replace(/<\/?(p|div|section|header|footer|nav|ul|ol|table|blockquote|br|main|article|aside)[^>]*>/gi, '\n')
    .replace(/<[^>]+>/g, '');
  const markdown = decode(md).replace(/[ \t]+/g, ' ').replace(/ *\n */g, '\n').replace(/\n{3,}/g, '\n\n').trim();
  const links = [...markdown.matchAll(/\[[^\]]*\]\((https?:[^)\s]+)\)/g)].map((m) => m[1]);
  return { title, markdown, links: [...new Set(links)] };
}
