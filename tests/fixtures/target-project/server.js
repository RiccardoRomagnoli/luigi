const http = require('http');
const { URL } = require('url');
const { divide } = require('./src/divide');

function html() {
  return `<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>Divide</title>
  </head>
  <body>
    <label>
      A
      <input id="a" type="number" />
    </label>
    <label>
      B
      <input id="b" type="number" />
    </label>
    <button id="divide">Divide</button>
    <pre id="result"></pre>

    <script>
      const $ = (id) => document.getElementById(id);
      $('divide').addEventListener('click', async () => {
        const a = $('a').value;
        const b = $('b').value;
        try {
          const res = await fetch('/api/divide?a=' + encodeURIComponent(a) + '&b=' + encodeURIComponent(b));
          const json = await res.json();
          if (!res.ok) {
            $('result').textContent = 'Error: ' + (json.error || 'unknown');
          } else {
            $('result').textContent = String(json.result);
          }
        } catch (e) {
          $('result').textContent = 'Error: ' + String(e && e.message ? e.message : e);
        }
      });
    </script>
  </body>
</html>`;
}

function startServer() {
  const server = http.createServer((req, res) => {
    const url = new URL(req.url || '/', 'http://127.0.0.1');
    if (url.pathname === '/') {
      res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
      res.end(html());
      return;
    }

    if (url.pathname === '/api/divide') {
      const a = Number(url.searchParams.get('a'));
      const b = Number(url.searchParams.get('b'));
      try {
        const result = divide(a, b);
        res.writeHead(200, { 'content-type': 'application/json; charset=utf-8' });
        res.end(JSON.stringify({ result }));
      } catch (e) {
        res.writeHead(400, { 'content-type': 'application/json; charset=utf-8' });
        res.end(JSON.stringify({ error: e && e.message ? e.message : String(e) }));
      }
      return;
    }

    res.writeHead(404, { 'content-type': 'text/plain; charset=utf-8' });
    res.end('Not found');
  });

  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => {
      const address = server.address();
      const port = address && typeof address === 'object' ? address.port : 0;
      resolve({
        server,
        baseURL: `http://127.0.0.1:${port}`,
      });
    });
  });
}

module.exports = { startServer };

