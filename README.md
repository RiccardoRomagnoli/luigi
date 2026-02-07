# Codex + Claude Code Automated Coding Orchestrator

Luigi orchestrates an automated agentic coding workflow:

- **Reviewers** produce a structured plan and select the best candidate implementation (default: **Codex CLI**).
- **Executors** implement the plan in a workspace (default: **Claude Code CLI**).
- Luigi runs plan-provided **test commands**, captures diffs/results, and iterates until approved (or a max-iteration limit).

Luigi supports:

- **Multi-agent** runs (multiple reviewers/executors, multiple candidates per iteration, consensus + admin tie-break).
- **Session mode** (keep the process alive and submit multiple tasks sequentially).
- **Resume** (continue a previous run by run id, or auto-resume the latest “running” run in UI-first mode).
- Optional **Streamlit UI** for monitoring + answering questions.
- Optional **Telegram** integration for admin decisions and end-of-run summaries.

## Safety notes (read this)

- **Luigi may execute arbitrary commands**: executors can run shell commands, and reviewers can output `test_commands` which Luigi runs verbatim.
- **Logs can contain sensitive data**: prompts, diffs, test output, and user answers are written to disk under `~/.luigi/logs/...`.
- **Prefer isolated workspaces** (`auto`, `worktree`, or `copy`) if you’re running against an important repo.

## Prerequisites

- Node.js 18+ (for the global `luigi` CLI wrapper)
- Python 3.10+
- Git (recommended; required for `workspace_strategy: worktree`)
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

If you want the Streamlit UI and/or YAML configs:

```bash
python3 -m pip install -r requirements.txt
```

## Usage

### Task-first (run in a repo immediately)

Run in the current directory:

```bash
luigi "Add an export-to-CSV feature. Use the project's existing test commands."
```

Run against a specific repo path:

```bash
luigi "Add an export-to-CSV feature." --repo /path/to/project
```

Create a new project (directory can be empty; it will be created if missing):

```bash
luigi "Create a Next.js app with Vitest + Playwright tests." --repo /path/to/new-project
```

### UI-first (enter the task in the web UI)

From inside a project folder:

```bash
luigi .
```

If Streamlit is installed, Luigi starts a UI and you can paste the initial task there. If Streamlit is not installed, Luigi falls back to terminal/file-based prompts.

### Session mode (multiple tasks, one long-running process)

The built-in defaults (`config.json` / `config.yaml`) ship with `orchestrator.session_mode: true`, which means Luigi will go **idle** after finishing a run and wait for another task (via UI / terminal / response file).

To exit session mode: Ctrl+C, or set `orchestrator.session_mode: false`.

### Resume a previous run

Resume a specific run id:

```bash
luigi /path/to/project --resume-run-id <run_id>
```

In UI-first mode (no explicit task), Luigi can also auto-resume the newest “running” run for the same repo if `orchestrator.resume_on_start: true`.

#### Resume semantics (stage-by-stage)

Luigi uses `state.json` to **resume exactly where it left off** in multi-agent runs:

- If `plans` already exist → planning is skipped.
- If `candidates` already exist → candidate workspaces are **reused**, and only incomplete candidates are executed.
- If `reviews` already exist → review is skipped and consensus/admin selection proceeds.

This is designed to survive interruptions without clobbering existing worktrees or copies.

### Choose the Python executable

The `luigi` Node wrapper uses `python3`/`python` by default. You can override:

```bash
LUIGI_PYTHON=/path/to/python3 luigi .
```

## Configuration

### Config file resolution (when `--config` is omitted)

Luigi searches the target repo for (in order):

```text
.luigi/config.json
.luigi/config.yaml
.luigi/config.yml
luigi.config.json
luigi.config.yaml
luigi.config.yml
```

If none are found, it uses the built-in `config.yaml` shipped with the package.

### Key orchestrator settings

- **`orchestrator.workspace_strategy`**: `in_place` | `auto` | `worktree` | `copy`
  - **`in_place`**: operate directly in the target directory (fastest; no isolation)
  - **`auto`**: use git worktree when possible, else copy
  - **`worktree`**: always use a git worktree on a new branch
  - **`copy`**: snapshot + work in a copy + apply back on approval
- **`orchestrator.cleanup`**: `on_success` (recommended) | `always` | `never`
- **`orchestrator.max_iterations`**: max plan/execute/test/review loops per task (`null`/`0` = unlimited)
- **`orchestrator.max_claude_question_rounds`**: max reviewer Q&A rounds when an agent requests clarification (`null`/`0` = unlimited)
- **`orchestrator.session_mode`**: keep Luigi running for multiple tasks
- **`orchestrator.resume_on_start`**: auto-resume newest “running” run when starting UI-first
- **`orchestrator.carry_forward_workspace_between_iterations`**: when an iteration is rejected, carry the selected candidate's changes into the next iteration (default: `true`)
- **`orchestrator.auto_merge_on_approval`**: automatically merge approved worktree branches into `merge_target_branch` (default: `false`)
- **`orchestrator.merge_target_branch`**: branch to merge into when auto-merge is enabled (default: `main`)
- **`orchestrator.merge_style`**: currently supports `merge_commit`
- **`orchestrator.dirty_main_policy`**: how to handle uncommitted changes on the target branch (`commit` or `abort`)
- **`orchestrator.delete_branch_on_merge`**: delete the local worktree branch after a successful auto-merge
- **`orchestrator.delete_worktree_on_merge`**: remove the worktree after a successful auto-merge
- **`orchestrator.branch_prefix`**: prefix for worktree branches (default: `luigi`)
- **`orchestrator.branch_name_length`**: run id length used in branch names (default: `8`)
- **`orchestrator.branch_suffix_length`**: candidate hash length for branch names (default: `6`)

#### Carry-forward behavior (multi-agent)

When an iteration is **rejected** but a winner is selected:

- If `carry_forward_workspace_between_iterations: true` (default), Luigi uses the selected candidate as the **baseline** for the next iteration.
  - The next iteration uses the **copy** strategy to safely carry forward uncommitted changes.
  - This ensures a rejected-but-best candidate can be refined without losing work.
- If `carry_forward_workspace_between_iterations: false`, the next iteration starts from the **original repo state**.

When an iteration is **approved**, Luigi persists changes (commit or apply) and does **not** start a new iteration for that task.

### Multi-agent configuration (`agents.*`)

Define multiple reviewers/executors (defaults are 1 reviewer + 1 executor):

```json
{
  "agents": {
    "reviewers": [
      { "id": "reviewer-1", "kind": "codex" }
    ],
    "executors": [
      { "id": "executor-1", "kind": "claude" }
    ],
    "assignment": {
      "executors_per_plan": 1
    }
  }
}
```

When multiple reviewers/executors are configured, Luigi can run **multiple candidates per iteration**. Reviewers pick a winner candidate and agree on APPROVED/REJECTED; if they disagree, Luigi requests an **admin decision** (UI / Telegram).

#### Worktree lifecycle

- **Non-selected candidates** are cleaned up immediately (best-effort).
- **Selected candidate** is kept until the end of the run and then cleaned up based on `orchestrator.cleanup`:
  - `always`: remove workspace/worktree
  - `on_success`: remove only if approved + persisted
  - `never`: keep everything

Note: Luigi removes worktree directories based on `orchestrator.cleanup`. If `auto_merge_on_approval` is enabled, it can also delete the worktree and local branch via `delete_worktree_on_merge` / `delete_branch_on_merge`. When auto-merge is disabled, Luigi leaves the short feature branch for manual admin merge.
When auto-merge is enabled and a conflict occurs, Luigi invokes Claude Code to resolve it using the approved plan and review context.

### Telegram integration (`telegram.*`) (optional)

Telegram is used for:

- Admin decision prompts (when reviewers disagree)
- End-of-run reviewer summaries (handoff)
- Session-mode task prompts (when the run returns to idle)

Example:

```json
{
  "telegram": {
    "enabled": true,
    "bot_token": "123:abc",
    "chat_id": "123456",
    "allowed_user_ids": [11111111],
    "poll_interval_sec": 2.0
  }
}
```

If `allowed_user_ids` is empty, **any user in the configured chat** can respond. Use an allowlist if you want to restrict who can send admin decisions.
For task prompts, Luigi sends a `request_id`; reply with that `request_id` and a `task:` line to start the next run.

### Testing settings (`testing.*`)

- If the plan provides `test_commands`, Luigi runs those.
- If the plan sets `test_commands: null`, Luigi falls back to:
  - `testing.unit_command` (default: `["npm","test"]`)
  - `testing.e2e_command` (default: `["npx","playwright","test"]`)
- `testing.timeout_sec` controls per-command timeouts (can also be overridden per test command).
- `testing.install_if_missing` can run `npm install` automatically if `package.json` exists and `node_modules` is missing.

## Monitoring / “what’s happening?”

Every run writes live-updating artifacts under `~/.luigi/logs/<run_id>/`:

- `state.json` (plans, candidates, test results, reviewer decisions, UI/admin prompts)
- `history.log`
- `codex.log`, `claude.log` (agent CLI logs)
- `streamlit.log` (if UI is running)

On startup the CLI prints the **Run ID**, **Logs path**, and **Workspace path**.

### Streamlit web UI

If Streamlit is installed, Luigi can start a UI while it runs:

- **One project = one port** (port is chosen deterministically from the folder name you invoked `luigi` from)
- **Multiple projects running** will naturally land on **different ports**
- If the preferred port is taken, Luigi will pick the next free port

The UI supports:

- Live state/log viewing (including a unified activity feed)
- Entering the initial task (UI-first mode)
- Answering user questions (Codex `NEEDS_USER_INPUT`)
- Submitting admin decisions (when reviewers disagree)

## What if an agent needs user input?

Luigi runs agents in non-interactive mode (`codex exec` / `claude -p`) but supports Q&A:

- **Executor needs clarification**: an executor returns `NEEDS_REVIEWER` + questions (Claude: `structured_output.status="NEEDS_REVIEWER"`). Luigi asks the configured reviewers (Codex/Claude), then resumes the executor with their answers. (Back-compat: `NEEDS_CODEX` is still accepted.)
- **Reviewer needs clarification**: a reviewer returns `{"status":"NEEDS_USER_INPUT","questions":[...]}`. Luigi surfaces questions in the UI (or in the terminal if no UI is running).
- **Admin decision required** (multi-agent disagreement): Luigi surfaces a chooser in the UI and can also send the decision prompt to Telegram (if enabled).

If Luigi is not running in an interactive terminal (no TTY) and needs input, you must answer via the UI (or by writing the response JSON into the run’s logs directory).

Tip: set `orchestrator.cleanup: never` when debugging so the workspace is always retained.

## Orchestrator tests (offline)

This repo includes offline unit + integration tests using mocked `codex` + `claude` CLIs:

```bash
python3 -m unittest discover -s tests_python -v
```

## More

See `ARCHITECTURE.md` for a deeper breakdown.
