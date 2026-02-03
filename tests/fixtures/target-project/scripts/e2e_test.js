const assert = require('assert');
const { startServer } = require('../server');

async function main() {
  const started = await startServer();
  const server = started.server;
  const baseURL = started.baseURL;

  try {
    const res = await fetch(`${baseURL}/api/divide?a=1&b=0`);
    const json = await res.json();
    assert.strictEqual(res.status, 400);
    assert.strictEqual(json && json.error, 'Division by zero');
    console.log('e2e ok');
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});

