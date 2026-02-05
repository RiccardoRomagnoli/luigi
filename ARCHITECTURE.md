# Codex + Claude Code Automated Coding Orchestrator

## Overview

This system implements an automated multi-agent coding workflow where **OpenAI Codex** serves as the architect (planning and code review) while **Claude Code CLI** serves as the implementer. The system iterates automatically until Codex approves the implementation.

The key advantage of using Claude Code CLI over direct API calls is that Claude Code has built-in capabilities for file editing, command execution, and codebase understanding—making it a true coding agent rather than just an LLM.

## Distribution / How you run it

The intended way to use this across many projects is as a **globally installed npm CLI**:

```bash
npm install -g .
luigi "..." --repo /path/to/project
```

The `luigi` Node wrapper shells out to the Python orchestrator (`main.py`) that lives inside the npm package.

## Streamlit UI (monitoring + interaction)

While Luigi runs, it can start a **Streamlit web UI** that:
- shows the live `state.json` / `history.log`
- displays the Codex plan, Claude status, test results, and Codex review
- lets you answer **Codex questions** from the browser (when Codex returns `NEEDS_USER_INPUT`)

Port assignment:
- The project identifier is the **folder name you invoked `luigi` from**
- The UI port is chosen deterministically from that identifier (with collision fallback)
- Multiple projects running concurrently should land on different ports

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    ORCHESTRATOR (Python/Bash)                   │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│   ┌─────────────┐         ┌─────────────┐         ┌──────────┐ │
│   │   CODEX     │         │ CLAUDE CODE │         │  STATE   │ │
│   │  (Planner)  │◄───────►│    CLI      │◄───────►│ MANAGER  │ │
│   │  (Reviewer) │         │ (Implementer)│         │          │ │
│   └─────────────┘         └─────────────┘         └──────────┘ │
│         │                       │                       │       │
│         ▼                       ▼                       ▼       │
│   ┌─────────────┐         ┌─────────────┐         ┌──────────┐ │
│   │ codex exec  │         │ claude -p   │         │ WORKSPACE│ │
│   │ (JSON schema│         │ (headless)  │         │ (worktree│ │
│   │  outputs)   │         │             │         │  / copy) │ │
│   └─────────────┘         └─────────────┘         └──────────┘ │
│                         ┌─────────────┐                         │
│                         │ TEST RUNNER │                         │
│                         │ (plan-driven│                         │
│                         │ commands)   │                         │
│                         └─────────────┘                         │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

## Workflow Loop

```
┌──────────────────────────────────────────────────────────────────┐
│                                                                  │
│  1. USER INPUT ──► 2. CODEX PLANNING ──► 3. CLAUDE CODE IMPL    │
│                           │                        │             │
│                           │                        ▼             │
│                           │              4. CODEX REVIEW         │
│                           │                        │             │
│                           │            ┌───────────┴──────────┐  │
│                           │            │                      │  │
│                           │      APPROVED?              REJECTED │
│                           │            │                      │  │
│                           │            ▼                      │  │
│                           │      5. COMPLETE           ───────┘  │
│                           │                     (loop back to 3) │
│                           │                                      │
└──────────────────────────────────────────────────────────────────┘
```

## Components

### 1. Codex Planner (OpenAI)
Uses Codex CLI to analyze the codebase and create a structured plan.

The plan is **machine-readable JSON** (validated by Codex via `--output-schema`) and includes:
- `claude_prompt`: the exact prompt for Claude Code CLI
- `tasks`: the task breakdown
- `test_commands`: the exact commands the orchestrator should run after implementation (unit tests, E2E, lint, etc.)

```bash
# Using Codex CLI
codex exec --model gpt-5.3-codex -c model_reasoning_effort=xhigh \
  --output-schema schemas/codex_plan.schema.json \
  --output-last-message /tmp/plan.json \
  "PHASE: PLAN ..."
```

### 2. Claude Code Implementer (Anthropic)
Uses Claude Code CLI in print mode to execute the implementation prompt produced by Codex.

```bash
# Non-interactive implementation
claude -p "$CLAUDE_PROMPT" \
  --model opus \
  --allowedTools "Bash,Read,Edit,Write,Glob,Grep" \
  --output-format json

# Continue previous session if needed
claude -p "Continue implementing" --continue --model opus
```

### 3. Codex Reviewer (OpenAI)
Reviews changes made by Claude Code and provides structured feedback.

```bash
# Using Codex CLI for review
codex exec --model gpt-5.3-codex -c model_reasoning_effort=xhigh \
  --output-schema schemas/codex_review.schema.json \
  --output-last-message /tmp/review.json \
  "PHASE: REVIEW ..."
```

### 4. Workspace Manager + Test Runner

- **Workspace isolation**:
  - If the target directory is a git repo with commits, the orchestrator can use a **git worktree** on a new branch.
  - Otherwise it can work in an isolated **copy** and then apply changes back on approval.
  - You can also run **in-place** (no isolation).
- **Testing**:
  - The orchestrator runs `test_commands` from the Codex plan.
  - Test results (exit codes + output) are sent back to Codex during review.

## Key CLI Commands

### Claude Code CLI (Implementer)

| Command | Purpose |
|---------|---------|
| `claude -p "prompt"` | Non-interactive mode |
| `--model opus` | Use Opus model |
| `--allowedTools "Bash,Read,Edit"` | Auto-approve tools |
| `--output-format json` | Get JSON with session_id |
| `--continue` | Continue last conversation |
| `--resume <session_id>` | Resume specific session |
| `--max-turns N` | Limit agentic turns |

### Codex CLI (Planner/Reviewer)

| Command | Purpose |
|---------|---------|
| `codex exec "prompt"` | Non-interactive mode |
| `codex exec resume --last "prompt"` | Continue previous session |
| `--model gpt-5.3-codex` | Specify model |

## Configuration

```yaml
# config.yaml
codex:
  model: "gpt-5.3-codex"
  reasoning_effort: "xhigh"
  
claude_code:
  model: "opus"
  allowed_tools:
    - "Bash"
    - "Read"
    - "Edit"
    - "Write"
    - "Glob"
    - "Grep"
  max_turns: 50
  
orchestrator:
  max_iterations: 5
  working_dir: "~/.luigi/workspaces"
  logs_dir: "~/.luigi/logs"
  workspace_strategy: "in_place"   # in_place | auto | worktree | copy
  use_git_worktree: true
  cleanup: "on_success"        # always | on_success | never
  apply_changes_on_success: true
  commit_on_approval: true
  auto_merge_on_approval: true
  merge_target_branch: "main"
  merge_style: "merge_commit"
  dirty_main_policy: "commit"
  dirty_main_commit_message: "Auto-commit local changes before Luigi merge (run {run_id})"
  merge_commit_message: "Merge {branch} into {target} (run {run_id})"
  delete_branch_on_merge: true
  delete_worktree_on_merge: true

testing:
  timeout_sec: 1800
```

## File Structure

```
luigi/
├── main.py               # Main orchestration loop
├── codex_client.py       # Codex CLI wrapper (schema-validated JSON)
├── claude_code_client.py # Claude Code CLI wrapper
├── workspace_manager.py  # worktree/copy/in-place workspace management
├── test_runner.py        # plan-driven test command runner
├── state_manager.py      # State and history logging
├── schemas/              # JSON schemas for Codex outputs
├── config.yaml           # Default YAML config (PyYAML)
├── requirements.txt      # Python deps (PyYAML for YAML configs)
└── package.json          # (optional) Node deps (Codex SDK usage)
```

## Exit Conditions

The loop terminates when:
1. Codex approves the implementation
2. Maximum iteration count is reached
3. User manually interrupts
4. Critical error occurs

## Error Handling

- API rate limits: Exponential backoff with retry
- Context overflow: Start fresh Claude Code session
- Git conflicts: Automatic stash and reapply
- Network errors: Retry with timeout
- Claude Code timeout: Use `--max-turns` to limit
