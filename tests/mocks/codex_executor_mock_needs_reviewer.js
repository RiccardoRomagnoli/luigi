const fs = require('fs');
const path = require('path');

function getArgValue(argv, flag) {
  const idx = argv.indexOf(flag);
  if (idx === -1) return null;
  return argv[idx + 1] ?? null;
}

function main() {
  const argv = process.argv.slice(2);
  const subcommand = argv[0];
  if (subcommand !== 'exec') {
    console.error('codex_executor_mock_needs_reviewer only supports: exec');
    process.exit(2);
  }

  // Respect Codex CLI-style "--cd" (CodexClient passes this flag).
  const cd = getArgValue(argv, '--cd');
  if (cd) {
    try {
      process.chdir(cd);
    } catch (e) {
      console.error(`codex_executor_mock_needs_reviewer: failed to chdir to ${cd}: ${e && e.message ? e.message : e}`);
      process.exit(2);
    }
  }

  const outputLastMessage = getArgValue(argv, '--output-last-message');
  if (!outputLastMessage) {
    console.error('Missing --output-last-message');
    process.exit(2);
  }

  const prompt = argv[argv.length - 1] || '';
  if (!prompt.includes('PHASE: EXECUTE')) {
    console.error('codex_executor_mock_needs_reviewer: expected PHASE: EXECUTE prompt');
    process.exit(2);
  }

  const cwd = process.cwd();
  const stateFile = path.join(cwd, '.codex_executor_mock_state_needs_reviewer');
  const target = path.join(cwd, 'src', 'divide.js');

  let response;

  // First turn: ask for reviewer input.
  if (!fs.existsSync(stateFile)) {
    fs.writeFileSync(stateFile, 'asked', 'utf8');
    response = {
      status: 'NEEDS_REVIEWER',
      questions: ['Should division by zero throw, or return null?'],
      summary: 'Need reviewer guidance on division-by-zero behavior.',
      notes: 'Mock executor asking reviewers (tests/mocks/codex_executor_mock_needs_reviewer.js)',
    };
  } else {
    if (!fs.existsSync(target)) {
      console.error(`codex_executor_mock_needs_reviewer: expected file missing: ${target}`);
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

    response = {
      status: 'DONE',
      questions: [],
      summary: 'Implemented throw on division by zero.',
      notes: 'Mock executor completed (tests/mocks/codex_executor_mock_needs_reviewer.js)',
    };
  }

  fs.writeFileSync(outputLastMessage, JSON.stringify(response, null, 2), 'utf8');
  process.exit(0);
}

main();

