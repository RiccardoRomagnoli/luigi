'''
# Codex + Claude Code Automated Coding Orchestrator: Complete Setup Guide

**Author:** Manus AI  
**Date:** February 1, 2026

---

## Introduction

This guide describes how to set up an automated multi-agent coding workflow where **OpenAI Codex** handles planning and code review, while the **Claude Code CLI** (running the powerful Opus model) executes the implementation. The system runs in an iterative loop until Codex is satisfied with the results, effectively creating a self-correcting development pipeline.

This updated approach uses the Claude Code CLI instead of direct API calls to Anthropic's models. The key advantage is that the Claude Code CLI is a true coding agent with built-in capabilities for file editing, command execution, and codebase understanding, providing a much richer and more integrated implementation environment.

---

## Architecture Overview

The orchestration system consists of four main components working together in a continuous loop.

| Component | Role | Technology |
|-----------|------|------------|
| **Codex Planner** | Creates detailed implementation plans from high-level task descriptions | OpenAI Codex SDK or `codex exec` CLI |
| **Claude Code Implementer** | Executes the plan by writing code and running commands | `claude -p` CLI (with `--model opus`) |
| **Codex Reviewer** | Reviews implementation and provides structured feedback | OpenAI Codex SDK or `codex exec` CLI |
| **State Manager** | Tracks iterations, history, and manages git worktrees | Python |

The workflow follows a simple but powerful pattern: **Plan → Implement → Review → (Iterate or Complete)**. This loop continues automatically until Codex approves the implementation or a maximum iteration count is reached.

---

## Prerequisites

Before setting up the orchestrator, ensure you have the following:

**Software Requirements:**
- Python 3.8 or higher
- Node.js 18 or higher (required for the Codex SDK)
- Git (for worktree management)
- **Claude Code CLI installed**

**API Access:**
- An OpenAI API key with access to Codex models (specifically `gpt-5-codex`)
- An Anthropic account with access to Claude Code and the Opus model.

---

## Installation

### Step 1: Clone or Create the Project

Create a new directory for the orchestrator and navigate into it:

```bash
mkdir codex-claude-orchestrator
cd codex-claude-orchestrator
```

### Step 2: Install Python Dependencies

Create a `requirements.txt` file with the following contents:

```
pyyaml
```

Then install the dependencies:

```bash
pip install -r requirements.txt
```

### Step 3: Install Node.js Dependencies (for Codex SDK)

Create a `package.json` file and install the Codex SDK:

```bash
npm init -y
npm install @openai/codex-sdk
```

### Step 4: Configure API Keys

Ensure your `OPENAI_API_KEY` is available as an environment variable. The Claude Code CLI will use your authenticated Anthropic account.

```bash
export OPENAI_API_KEY="sk-your-openai-api-key"
```

---

## Configuration

The orchestrator uses a `config.yaml` file to manage settings:

```yaml
codex:
  model: "gpt-5-codex"

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
  working_dir: "./workspace"
```

| Setting | Description | Default |
|---------|-------------|---------|
| `codex.model` | The Codex model to use for planning and review | `gpt-5-codex` |
| `claude_code.model` | The Claude model to use for implementation (`opus` or `sonnet`) | `opus` |
| `claude_code.allowed_tools` | List of tools the Claude agent can use without prompting | `["Bash", "Read", "Edit", ...]` |
| `claude_code.max_turns` | Limit on the number of agentic turns for Claude Code | `50` |
| `orchestrator.max_iterations` | Maximum review-implement cycles before giving up | `5` |
| `orchestrator.working_dir` | Base directory for git worktrees | `./workspace` |
| `orchestrator.auto_merge_on_approval` | Auto-merge approved worktree branches into the target branch | `true` |
| `orchestrator.merge_target_branch` | Branch to merge into | `main` |
| `orchestrator.merge_style` | Merge strategy (`merge_commit`) | `merge_commit` |
| `orchestrator.dirty_main_policy` | How to handle uncommitted target branch changes (`commit` or `abort`) | `commit` |
| `orchestrator.delete_branch_on_merge` | Delete worktree branch after successful merge | `true` |
| `orchestrator.delete_worktree_on_merge` | Remove worktree after successful merge | `true` |

---

## How the Loop Works

The orchestration loop is driven by the `main.py` script and follows these steps:

### 1. Planning Phase (Codex)

Codex receives the task description and creates a detailed, structured implementation plan.

```python
# codex_client.py
plan = codex_client.create_plan(task)
```

### 2. Implementation Phase (Claude Code CLI)

The `claude_code_client.py` module invokes the Claude Code CLI in non-interactive (`-p`) mode, passing the plan as the prompt. It uses the Opus model and auto-approves a set of essential tools.

```python
# claude_code_client.py
command = [
    "claude", "-p", plan,
    "--model", self.config["model"],
    "--output-format", "json",
    "--allowedTools", ",".join(self.config["allowed_tools"])
]
result = subprocess.run(command, ...)
```

The output is captured as JSON, which includes the `session_id` for potential follow-up interactions.

### 3. Review Phase (Codex)

Codex reviews the implementation result (or the git diff of the worktree) and provides structured feedback.

```json
{
  "status": "APPROVED" | "REJECTED",
  "feedback": "Detailed explanation of issues or approval"
}
```

### 4. Iteration or Completion

If the review status is `APPROVED`, the loop terminates. If `REJECTED`, the feedback is used to refine the plan, and the `claude_code_client` is invoked again, resuming the previous session to provide context.

---

## Best Practices

**Use Git Worktrees for Isolation:** Each task runs in its own git worktree to prevent interference and allow for easy cleanup of failed attempts.

**Leverage Session Resumption:** The `claude_code_client` should capture and reuse the `session_id` to allow Claude Code to maintain context across multiple implementation attempts within the same task.

**Auto-Approve Essential Tools:** The `--allowedTools` flag is critical for automation. Grant access to file system operations (`Read`, `Write`, `Edit`, `Glob`, `Grep`) and `Bash` to enable the agent to work effectively.

**Set Resource Limits:** Use `--max-turns` and `--max-budget-usd` in the Claude Code CLI to prevent runaway processes and control costs.

---

## References

[1] Anthropic, "Run Claude Code programmatically," https://code.claude.com/docs/en/headless

[2] Anthropic, "CLI reference," https://code.claude.com/docs/en/cli-reference

[3] OpenAI, "Codex CLI features," https://developers.openai.com/codex/cli/features/

[4] Jesse Vincent, "How I'm using coding agents in September, 2025," https://blog.fsck.com/2025/10/05/how-im-using-coding-agents-in-september-2025/
'''
