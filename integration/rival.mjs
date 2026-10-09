import http from 'node:http';
const pages = {
  '/': '<title>Rival Co | Home</title><meta name="description" content="Rival Co sells widgets for busy teams."><nav><a href="/pricing">Pricing</a> <a href="/about">About</a> <a href="/private/secret">Secret</a></nav><h1>Widgets for busy teams</h1><p>Rival Co makes fast, friendly widgets.</p><footer><a href="mailto:hi@rival.example">hi@rival.example</a> <a href="https://x.com/rivalco">X</a></footer>',
  '/pricing': '<title>Pricing | Rival Co</title><h1>Pricing</h1><ul><li><strong>Widget Mini</strong> — $12/mo</li><li><strong>Widget Max</strong> — $48/mo</li></ul>',
  '/about': '<title>About | Rival Co</title><h1>About</h1><p>We started in 2020 with a simple idea about widgets.</p>',
  '/private/secret': '<title>Secret</title><h1>Do not crawl</h1>',
};
let price = 12; // POST /__edit lowers the price so we can see a change detected through the backend crawler
http.createServer((req, res) => {
  if (req.url === '/robots.txt') { res.writeHead(200, { 'content-type': 'text/plain' }); return res.end('User-agent: *\nDisallow: /private/\n'); }
  if (req.url === '/__edit' && req.method === 'POST') { price = 9; res.writeHead(200); return res.end('ok'); }
  let body = pages[req.url.split('?')[0]]; if (!body) { res.writeHead(404); return res.end('nope'); }
  body = body.replace('$12/mo', `$${price}/mo`);
  res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' }); res.end(`<!doctype html><html><head></head><body>${body}</body></html>`);
}).listen(3999, '127.0.0.1');
