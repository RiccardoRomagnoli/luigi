#!/usr/bin/env node

const { spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');

function tryCommand(cmd, args) {
  const res = spawnSync(cmd, args, { stdio: 'ignore' });
  return res.status === 0;
}

function findPythonExecutable() {
  const override = process.env.COMBO_AGENTS_PYTHON;
  const candidates = override ? [override] : ['python3', 'python'];
  for (const candidate of candidates) {
    if (tryCommand(candidate, ['--version'])) return candidate;
  }
  return null;
}

function main() {
  const python = findPythonExecutable();
  if (!python) {
    console.error(
      [
        'combo-agents: Python not found.',
        'Install python3 or set COMBO_AGENTS_PYTHON to your Python executable.',
      ].join('\n')
    );
    process.exit(1);
  }

  const mainPy = path.resolve(__dirname, '..', 'main.py');
  if (!fs.existsSync(mainPy)) {
    console.error(`combo-agents: missing main.py at ${mainPy}`);
    process.exit(1);
  }

  const argv = process.argv.slice(2);
  const forwarded = argv.length === 0 ? ['-h'] : argv;

  const res = spawnSync(python, [mainPy, ...forwarded], { stdio: 'inherit' });
  process.exit(res.status ?? 1);
}

main();

