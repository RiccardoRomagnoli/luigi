function divide(a, b) {
  // BUG (intentional for orchestrator E2E): should throw on division by zero.
  return a / b;
}

module.exports = { divide };

