# Codex + Claude Code Automated Coding Orchestrator

This project orchestrates an automated two-agent coding workflow:

- **Codex (GPT‑5.2, reasoning xhigh)** defines tasks (planning) and reviews results.
- **Claude Code CLI** executes the plan by editing files and running commands.

The orchestrator loops until Codex approves or a max-iteration limit is reached.

## Key behavior

- **Codex defines the work**: it outputs a structured plan containing:
  - `claude_prompt`: a complete prompt for Claude Code CLI
  - `tasks`: the task breakdown
  - `test_commands`: the exact validation commands the orchestrator should run
- **Claude implements** using the provided prompt
- **The orchestrator runs `test_commands`** (unit, E2E, lint, etc.)
- **Codex reviews** the code diff + test results and can add more tasks via plan refinement

Testing frameworks (e.g. **vitest** for unit and **Playwright** for E2E) are **prompt/plan-driven**: Codex should choose what fits the target project (or what you explicitly request).

## Prerequisites

- Python 3.10+
- Git (optional but recommended for worktrees/branches)
- The **Codex CLI** (`codex`) installed and authenticated
- The **Claude Code CLI** (`claude`) installed and authenticated

## Installation

### Install as a global CLI (recommended)

From this repo:

```bash
npm install -g .
```

This installs the `luigi` command globally.

### Python dependencies (optional)

The default config is `config.json`, so **no Python packages are required** (but the web UI will be disabled).

To enable the Streamlit UI (and to support YAML configs), install the Python deps:

```bash
python3 -m pip install -r requirements.txt
```

## Usage

### UI-first mode (enter the task in the web UI)

From inside a project folder:

```bash
luigi .
```

This starts Luigi + the Streamlit UI. Enter the initial task in the browser to begin.

Run on an **existing project**:

```bash
luigi "Add an export-to-CSV feature. Use Vitest for unit tests and Playwright for E2E." --repo /path/to/project
```

Create a **new project** (the directory can be empty; it will be created if missing):

```bash
luigi "Create a Next.js app with Vitest + Playwright tests." --repo /path/to/new-project
```

Use a custom config (YAML or JSON):

```bash
luigi "..." --repo /path/to/project --config /path/to/config.yaml
luigi "..." --repo /path/to/project --config /path/to/config.json
```

Repo-local configs (auto-detected if `--config` is omitted):

```bash
# JSON (recommended)
/path/to/project/.luigi/config.json
/path/to/project/luigi.config.json

# YAML (requires PyYAML)
/path/to/project/.luigi/config.yaml
/path/to/project/luigi.config.yaml
```

## Configuration

Built-in defaults ship in `config.json`. Highlights:

- `codex.model`: recommended default is `gpt-5.2-codex`
- `codex.reasoning_effort`: set to `xhigh` for maximum reasoning
- `orchestrator.workspace_strategy`:
  - `in_place` (default): run directly in the target directory (no isolation)
  - `auto`: git worktree when possible, otherwise snapshot+copy
  - `copy`: always run in an isolated copy (applied back on approval)
- `orchestrator.cleanup`: `on_success` (recommended), `always`, or `never`

## Monitoring / “what’s happening between Codex and Claude?”

Every run writes a live-updating state file:

- `~/.luigi/logs/<run_id>/state.json`
- `~/.luigi/logs/<run_id>/history.log`

`state.json` includes the **exact Codex→Claude handoff** (`plan.claude_prompt`), the plan’s
**test commands** (`plan.test_commands`), the **test results** (exit codes + output), and
Codex’s **review decision** (`review`).

On startup the CLI prints the **Run ID**, **Logs path**, and **Workspace path** so you can open
those files immediately.

### Streamlit web UI (recommended)

If Streamlit is installed, Luigi auto-starts a Streamlit UI while it runs and prints the URL:

- **One project = one port** (port is chosen deterministically from the folder name you invoked `luigi` from)
- **Multiple projects running** will naturally land on **different ports**
- If the preferred port is taken, Luigi will pick the next free port

## What if Codex or Claude asks for user input?

Luigi runs both agents in **headless / non-interactive mode** (`codex exec` and `claude -p`),
but it supports **interactive user Q&A** when Codex needs clarification.

Typical outcomes:

- **Claude needs clarification**: Claude returns `structured_output.status="NEEDS_CODEX"` and questions. Luigi sends those
  questions to Codex, then resumes Claude with Codex’s answer.
- **Codex needs clarification** (planning, review, or answering Claude): Codex returns `{"status":"NEEDS_USER_INPUT","questions":[...]}`.
  Luigi will surface the questions in the **Streamlit UI** (and will also prompt in the terminal if no UI is running).

If Luigi is not running in an interactive terminal (no TTY) and Codex needs user input, you must answer via the Streamlit UI
(or by writing the response file in the run’s logs directory).

Tip: set `orchestrator.cleanup: never` when debugging so the workspace is always retained.

## Orchestrator tests (offline)

This repo includes a fully offline integration test using mocked `codex` + `claude` CLIs:

```bash
python3 -m unittest discover -s tests_python -v
```

## More

See `ARCHITECTURE.md` for a deeper breakdown.
