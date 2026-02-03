const assert = require('assert');
const { divide } = require('../src/divide');

assert.strictEqual(divide(10, 2), 5);

let threw = false;
try {
  divide(1, 0);
} catch (e) {
  threw = true;
  assert.strictEqual(e && e.message, 'Division by zero');
}

if (!threw) {
  throw new Error('Expected divide(1, 0) to throw');
}

console.log('unit ok');

