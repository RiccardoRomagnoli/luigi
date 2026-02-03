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
  const stateFile = path.join(cwd, '.claude_mock_state');
  const target = path.join(cwd, 'src', 'divide.js');

  // First turn: ask Codex for clarification.
  if (!fs.existsSync(stateFile)) {
    fs.writeFileSync(stateFile, 'asked', 'utf8');
    const output = {
      session_id: 'mock-session-needs-codex',
      result: `claude_mock_needs_codex: need clarification. Prompt length: ${prompt.length}`,
      structured_output: {
        status: 'NEEDS_CODEX',
        questions: ['Should division by zero throw, or return null?'],
        summary: 'Need decision on behavior for division by zero.',
      },
    };
    process.stdout.write(JSON.stringify(output));
    return;
  }

  // Second turn: implement using Codex's answer (we ignore content here and just implement throw).
  if (!fs.existsSync(target)) {
    console.error(`claude_mock_needs_codex: expected file missing: ${target}`);
    process.exit(2);
  }

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
    session_id: 'mock-session-needs-codex',
    result: `claude_mock_needs_codex: implemented. Prompt length: ${prompt.length}`,
    structured_output: {
      status: 'DONE',
      summary: 'Implemented throw on division by zero.',
    },
  };
  process.stdout.write(JSON.stringify(output));
}

main();

