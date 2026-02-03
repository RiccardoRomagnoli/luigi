const fs = require('fs');
const path = require('path');

function getArgValue(argv, flag) {
  const idx = argv.indexOf(flag);
  if (idx === -1) return null;
  return argv[idx + 1] ?? null;
}

function main() {
  const argv = process.argv.slice(2);
  const prompt = getArgValue(argv, '-p') || '';

  const cwd = process.cwd();
  const target = path.join(cwd, 'src', 'divide.js');

  if (!fs.existsSync(target)) {
    console.error(`claude_mock: expected file missing: ${target}`);
    process.exit(2);
  }

  // Minimal "implementation": ensure divide() throws on b===0.
  const next = [
    'function divide(a, b) {',
    '  if (b === 0) {',
    '    throw new Error("Division by zero");',
    '  }',
    '  return a / b;',
    '}',
    '',
    'module.exports = { divide };',
    '',
  ].join('\n');

  fs.writeFileSync(target, next, 'utf8');

  const output = {
    session_id: 'mock-session',
    result: `claude_mock applied fix. Prompt length: ${prompt.length}`,
  };

  process.stdout.write(JSON.stringify(output));
}

main();

