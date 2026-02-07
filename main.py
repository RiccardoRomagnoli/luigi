
import argparse
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from agents import AgentSpec, assignment_config, normalize_agents
from telegram_client import TelegramClient
from codex_client import CodexClient
from claude_code_client import ClaudeCodeClient
from state_manager import StateManager
from workspace_manager import WorkspaceManager, Workspace
from test_runner import run_tests
from ui_server import compute_project_id, start_streamlit_ui


def load_config(path: str):
    """Load configuration from JSON or YAML.

    YAML requires PyYAML; JSON uses only the standard library.
    """
    if path.endswith(".json"):
        with open(path, "r") as f:
            return json.load(f)

    # Default: YAML
    try:
        import yaml  # type: ignore
    except ModuleNotFoundError as e:
        raise SystemExit(
            "PyYAML is required to read YAML config files.\n"
            "Install dependencies with: pip install -r requirements.txt\n"
            "Or provide a JSON config via: --config config.json"
        ) from e

    with open(path, "r") as f:
        return yaml.safe_load(f)


def _normalize_path(path: str, *, repo_path: str) -> str:
    expanded = os.path.expandvars(os.path.expanduser(path))
    if os.path.isabs(expanded):
        return expanded
    return os.path.abspath(os.path.join(repo_path, expanded))


def _optional_positive_int(value: Any, *, default: Optional[int]) -> Optional[int]:
    """Return a positive int or None (meaning unlimited).

    - None -> None
    - <=0 -> None
    - unparsable -> default
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return default
    try:
        n = int(value)
    except Exception:
        return default
    if n <= 0:
        return None
    return n


def _read_json_file(path: str) -> dict | None:
    try:
        with open(path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        bak_path = f"{path}.bak"
        try:
            with open(bak_path, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return None


def _load_schema(path: str) -> dict:
    with open(path, "r") as f:
        return json.load(f)


def _schema_dir() -> str:
    return os.path.join(os.path.dirname(__file__), "schemas")


def _reviewer_decision_schema_path() -> str:
    return os.path.join(_schema_dir(), "reviewer_decision.schema.json")


def _executor_result_schema_path() -> str:
    return os.path.join(_schema_dir(), "executor_result.schema.json")


def _build_codex_client_for_agent(spec: AgentSpec, base_cfg: Dict[str, Any], log_dir: str) -> CodexClient:
    cfg = dict(base_cfg or {})
    if spec.command:
        cfg["command"] = spec.command
    if spec.model:
        cfg["model"] = spec.model
    if spec.reasoning_effort:
        cfg["reasoning_effort"] = spec.reasoning_effort
    if spec.sandbox:
        cfg["sandbox"] = spec.sandbox
    elif spec.role == "executor":
        cfg["sandbox"] = "workspace-write"
    if spec.approval_policy:
        cfg["approval_policy"] = spec.approval_policy
    cfg["log_dir"] = log_dir
    return CodexClient(cfg)


def _build_claude_client_for_agent(spec: AgentSpec, base_cfg: Dict[str, Any], log_dir: str) -> ClaudeCodeClient:
    cfg = dict(base_cfg or {})
    if spec.command:
        cfg["command"] = spec.command
    if spec.model:
        cfg["model"] = spec.model
    if spec.allowed_tools is not None:
        cfg["allowed_tools"] = spec.allowed_tools
    if spec.max_turns is not None:
        cfg["max_turns"] = spec.max_turns
    cfg["log_dir"] = log_dir
    return ClaudeCodeClient(cfg)


def _parse_admin_choice(text: str) -> Dict[str, Any]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    choice = None
    request_id = None
    notes_lines: List[str] = []
    for line in lines:
        lower = line.lower()
        if lower.startswith("request_id:") or lower.startswith("request-id:") or lower.startswith("request id:"):
            request_id = line.split(":", 1)[1].strip()
        elif lower.startswith("request "):
            request_id = line.split(" ", 1)[1].strip()
        elif lower.startswith("choose "):
            try:
                choice = int(lower.replace("choose", "").strip())
            except ValueError:
                continue
        elif lower.startswith("notes:"):
            notes_lines.append(line.split(":", 1)[1].strip())
        else:
            notes_lines.append(line)
    return {"choice": choice, "notes": "\n".join(notes_lines).strip(), "request_id": request_id}


def _parse_task_message(text: str) -> Dict[str, Any]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    request_id = None
    task_lines: List[str] = []
    for line in lines:
        lower = line.lower()
        if lower.startswith("request_id:") or lower.startswith("request-id:") or lower.startswith("request id:"):
            request_id = line.split(":", 1)[1].strip()
        elif lower.startswith("request "):
            request_id = line.split(" ", 1)[1].strip()
        elif lower.startswith("task:"):
            task_lines.append(line.split(":", 1)[1].strip())
        elif lower.startswith("task "):
            task_lines.append(line.split(" ", 1)[1].strip())
        else:
            task_lines.append(line)
    return {"request_id": request_id, "task": "\n".join(task_lines).strip()}


def _send_telegram_message(
    *,
    state_manager: StateManager,
    telegram: Optional[TelegramClient],
    text: str,
    label: str,
) -> bool:
    if not telegram:
        return False
    ok, error = telegram.send_message(text, return_error=True)
    if ok:
        msg = f"Telegram sent ({label})."
    else:
        msg = f"Telegram send failed ({label}): {error or 'unknown error'}"
    print(msg)
    state_manager.add_to_history(msg)
    return ok


def _claude_plan_prompt(task: str, *, user_context: str) -> str:
    return (
        "PHASE: PLAN\n"
        "You are a reviewer planning the work. Output JSON that matches the plan schema exactly.\n"
        "IMPORTANT: You are running in restricted tool mode (Read/Glob/Grep only). Do NOT attempt to run shell commands.\n"
        "- Always include ALL top-level fields: status, claude_prompt, tasks, test_commands, questions, notes.\n"
        '- For a normal plan, set status to "OK". For clarification, set status to "NEEDS_USER_INPUT".\n'
        "- Use null or empty arrays for fields that do not apply.\n"
        "- Keep tasks incremental and testable.\n"
        "- Include test_commands only if the project already has tests; otherwise set null.\n\n"
        f"User task:\n{task}\n"
        + (f"\nUser context / answers:\n{user_context}\n" if user_context else "")
    )


def _review_candidates_prompt(
    *,
    task: str,
    candidates_text: str,
    user_context: str,
    final_handoff: bool,
) -> str:
    phase = "HANDOFF" if final_handoff else "REVIEW_CANDIDATES"
    return (
        f"PHASE: {phase}\n"
        "You are a reviewer. Choose the best candidate and decide whether the task is done.\n"
        "IMPORTANT: You are running in restricted tool mode (Read/Glob/Grep only). Do NOT attempt to run shell commands.\n"
        "Output JSON matching the reviewer_decision schema:\n"
        "- Always include: status, winner_candidate_id, summary, feedback, next_prompt, questions, notes.\n"
        '- If you need clarification from the admin, set status to "NEEDS_USER_INPUT" and add questions.\n'
        "- Otherwise use status APPROVED or REJECTED.\n"
        "CRITICAL semantics:\n"
        '- status="APPROVED" means Luigi will STOP iterating and persist/commit the selected candidate.\n'
        '- Only set APPROVED if all user requirements are fully satisfied.\n'
        '- If ANY required work remains (missing features, bugs, failing tests, or unverified claims), set status="REJECTED".\n'
        '- If status is APPROVED, set next_prompt to null.\n'
        "- If status is REJECTED, next_prompt should be a concise prompt for the next iteration.\n"
        "- winner_candidate_id must be one of the candidates.\n"
        "- summary: short admin-facing summary of what happened.\n"
        "- feedback: concrete guidance; include remaining work here if REJECTED.\n"
        "- next_prompt: prompt for next iteration (null if APPROVED).\n\n"
        f"User task:\n{task}\n\n"
        f"Candidates:\n{candidates_text}\n\n"
        + (f"User context / answers:\n{user_context}\n" if user_context else "")
    )


def _validate_reviewer_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Minimal plan-shape validation for reviewer-produced plans (Codex or Claude)."""
    if not isinstance(plan, dict):
        raise RuntimeError("Reviewer plan invalid: expected an object.")

    status = plan.get("status")
    if status == "NEEDS_USER_INPUT":
        questions = plan.get("questions", [])
        if not isinstance(questions, list) or not questions:
            raise RuntimeError("Reviewer plan invalid: NEEDS_USER_INPUT requires questions.")
        return plan

    if status != "OK":
        raise RuntimeError(f"Reviewer plan invalid: unknown status {status!r}.")

    claude_prompt = plan.get("claude_prompt")
    if not isinstance(claude_prompt, str) or not claude_prompt.strip():
        raise RuntimeError("Reviewer plan invalid: claude_prompt must be a non-empty string.")

    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise RuntimeError("Reviewer plan invalid: tasks must be a non-empty list.")
    for idx, task in enumerate(tasks):
        if not isinstance(task, dict):
            raise RuntimeError(f"Reviewer plan invalid: tasks[{idx}] must be an object.")
        for field in ("id", "title", "description"):
            val = task.get(field)
            if not isinstance(val, str) or not val.strip():
                raise RuntimeError(
                    f"Reviewer plan invalid: tasks[{idx}].{field} must be a non-empty string."
                )

    return plan


def _await_admin_decision(
    *,
    state_manager: StateManager,
    options: List[Dict[str, Any]],
    ui_active: bool,
    telegram: Optional[TelegramClient],
    poll_interval_sec: float = 1.0,
    timeout_sec: Optional[float] = None,
) -> Dict[str, Any]:
    request_id = str(uuid.uuid4())
    state_manager.update_state(
        "awaiting_admin_decision",
        {"request_id": request_id, "options": options},
    )
    state_manager.add_to_history(f"Awaiting admin decision request_id={request_id} ({len(options)} options).")
    request_path = os.path.join(state_manager.log_dir, f"admin_decision_request_{request_id}.json")
    response_path = os.path.join(state_manager.log_dir, f"admin_decision_response_{request_id}.json")
    with open(request_path, "w") as f:
        json.dump({"request_id": request_id, "options": options}, f, indent=2)

    if ui_active:
        print("Admin decision required. Please answer in the Luigi web UI.")

    if telegram:
        lines = [
            "Admin decision required. Reply with:",
            f"request_id: {request_id}",
            "choose <N>",
            "notes: <optional>",
        ]
        for idx, opt in enumerate(options, start=1):
            lines.append(f"{idx}) {opt.get('label', '')}")
        _send_telegram_message(
            state_manager=state_manager,
            telegram=telegram,
            text="\n".join(lines),
            label=f"admin_decision_request:{request_id}",
        )

    start = time.time()
    offset = state_manager.get_state("telegram_update_offset")
    if not isinstance(offset, int):
        offset = None
    while True:
        # UI response file
        if os.path.exists(response_path):
            try:
                with open(response_path, "r") as f:
                    payload = json.load(f)
            except json.JSONDecodeError:
                payload = None
            if not isinstance(payload, dict):
                time.sleep(poll_interval_sec)
                continue
            try:
                os.remove(response_path)
            except OSError:
                pass
            choice = payload.get("choice")
            notes = payload.get("notes", "")
            state_manager.update_state("awaiting_admin_decision", None)
            return {"choice": choice, "notes": notes, "source": "ui"}

        # Telegram polling
        if telegram:
            updates = telegram.poll_updates(offset)
            for item in updates.get("result", []):
                update_id = item.get("update_id")
                if isinstance(update_id, int):
                    next_offset = update_id + 1
                    if offset is None or next_offset > offset:
                        offset = next_offset
                        state_manager.update_state("telegram_update_offset", offset)
            for message in telegram.filter_messages(updates):
                text = str(message.get("text", "")).strip()
                if not text:
                    continue
                parsed = _parse_admin_choice(text)
                if parsed.get("choice") and parsed.get("request_id") == request_id:
                    state_manager.update_state("awaiting_admin_decision", None)
                    return {"choice": parsed.get("choice"), "notes": parsed.get("notes", ""), "source": "telegram"}

        if timeout_sec is not None and (time.time() - start) > timeout_sec:
            raise RuntimeError("Timed out waiting for admin decision.")
        time.sleep(poll_interval_sec)


def _preview_one_line(text: str, *, max_len: int = 220) -> str:
    """Format a long multi-line text as a short one-line preview."""
    compact = " ".join((text or "").strip().split())
    if not compact:
        return ""
    if len(compact) <= max_len:
        return compact
    return compact[: max(0, max_len - 1)] + "…"


def _assign_executors(
    reviewer_ids: List[str],
    executors: List[AgentSpec],
    *,
    executors_per_plan: int,
) -> List[Dict[str, Any]]:
    assignments: List[Dict[str, Any]] = []
    if not executors or not reviewer_ids:
        return assignments
    index = 0
    for reviewer_id in reviewer_ids:
        for _ in range(executors_per_plan):
            executor = executors[index % len(executors)]
            index += 1
            assignments.append({"reviewer_id": reviewer_id, "executor": executor})
    return assignments


def _summarize_test_results(test_results: Dict[str, Any]) -> str:
    commands = test_results.get("commands") if isinstance(test_results, dict) else None
    if not isinstance(commands, list) or not commands:
        return "No tests were run."
    parts = []
    for cmd in commands:
        result = cmd.get("result", {})
        exit_code = result.get("exit_code")
        label = cmd.get("label") or cmd.get("id") or "test"
        parts.append(f"{label}: exit {exit_code}")
    return "; ".join(parts)


def _candidate_summary_text(candidate: Dict[str, Any]) -> str:
    lines = [
        f"candidate_id: {candidate.get('id')}",
        f"reviewer_id: {candidate.get('reviewer_id')}",
        f"executor_id: {candidate.get('executor_id')}",
        f"status: {candidate.get('status')}",
    ]
    if candidate.get("test_summary"):
        lines.append(f"tests: {candidate.get('test_summary')}")
    if candidate.get("executor_summary"):
        lines.append(f"executor_summary: {candidate.get('executor_summary')}")
    if candidate.get("diff_preview"):
        lines.append("diff_preview:")
        lines.append(candidate.get("diff_preview"))
    return "\n".join(lines)


def _validate_reviewer_decision(decision: Dict[str, Any], candidate_ids: set[str]) -> Dict[str, Any]:
    if not isinstance(decision, dict):
        raise RuntimeError("Reviewer decision invalid: expected an object.")
    status = decision.get("status")
    if status == "NEEDS_USER_INPUT":
        questions = decision.get("questions", [])
        if not isinstance(questions, list) or not questions:
            raise RuntimeError("Reviewer decision invalid: NEEDS_USER_INPUT requires questions.")
        return decision
    if status not in ("APPROVED", "REJECTED"):
        raise RuntimeError(f"Reviewer decision invalid: unknown status {status!r}.")
    winner = decision.get("winner_candidate_id")
    if not isinstance(winner, str) or not winner.strip():
        raise RuntimeError(
            "Reviewer decision invalid: winner_candidate_id must be a non-empty string when approved/rejected."
        )
    if winner not in candidate_ids:
        raise RuntimeError(
            f"Reviewer decision invalid: winner_candidate_id {winner!r} not in candidates."
        )
    # Guardrail: APPROVED must not carry a "next iteration" prompt.
    # If a reviewer provides a non-empty next_prompt, they are implicitly indicating remaining work,
    # which should be expressed as REJECTED instead.
    if status == "APPROVED":
        next_prompt = decision.get("next_prompt")
        if isinstance(next_prompt, str) and next_prompt.strip():
            raise RuntimeError(
                "Reviewer decision invalid: status=APPROVED requires next_prompt=null. "
                "If work remains, use status=REJECTED and provide next_prompt."
            )
    return decision


def _compute_consensus(decisions: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    winner = None
    next_prompt = None
    status = None
    for decision in decisions.values():
        if decision.get("status") == "NEEDS_USER_INPUT":
            return {"consensus": False, "winner": None, "next_prompt": None, "status": None}
        if winner is None:
            winner = decision.get("winner_candidate_id")
            next_prompt = decision.get("next_prompt")
            status = decision.get("status")
        else:
            if (
                decision.get("status") != status
                or decision.get("winner_candidate_id") != winner
                or decision.get("next_prompt") != next_prompt
            ):
                return {"consensus": False, "winner": None, "next_prompt": None, "status": None}
    if not winner:
        return {"consensus": False, "winner": None, "next_prompt": None, "status": None}
    return {"consensus": True, "winner": winner, "next_prompt": next_prompt, "status": status}


def _run_reviewer_plan(
    reviewer: AgentSpec,
    *,
    codex_clients: Dict[str, CodexClient],
    claude_clients: Dict[str, ClaudeCodeClient],
    task: str,
    user_context: str,
    cwd: str,
    plan_schema: Dict[str, Any],
) -> Dict[str, Any]:
    if reviewer.kind == "codex":
        return codex_clients[reviewer.id].create_plan(task, user_context=user_context, cwd=cwd)
    prompt = _claude_plan_prompt(task, user_context=user_context)
    output = claude_clients[reviewer.id].run_structured(
        prompt=prompt,
        json_schema=plan_schema,
        cwd=cwd,
        allowed_tools_override=["Read", "Glob", "Grep"],
    )
    if not output:
        raise RuntimeError("Claude reviewer failed to produce a plan.")
    return output


def _run_reviewer_decision(
    reviewer: AgentSpec,
    *,
    codex_clients: Dict[str, CodexClient],
    claude_clients: Dict[str, ClaudeCodeClient],
    prompt: str,
    cwd: str,
    decision_schema: Dict[str, Any],
) -> Dict[str, Any]:
    if reviewer.kind == "codex":
        return codex_clients[reviewer.id].run_structured(
            prompt=prompt,
            schema_path=_reviewer_decision_schema_path(),
            cwd=cwd,
        )
    output = claude_clients[reviewer.id].run_structured(
        prompt=prompt,
        json_schema=decision_schema,
        cwd=cwd,
        allowed_tools_override=["Read", "Glob", "Grep"],
    )
    if not output:
        raise RuntimeError("Claude reviewer failed to produce a decision.")
    return output


def _run_executor_candidate(
    *,
    executor: AgentSpec,
    plan: Dict[str, Any],
    codex_clients: Dict[str, CodexClient],
    claude_clients: Dict[str, ClaudeCodeClient],
    workspace: "Workspace",
    executor_schema: Dict[str, Any],
    append_system_prompt: str,
) -> Dict[str, Any]:
    if executor.kind == "claude":
        output = claude_clients[executor.id].implement(
            plan,
            cwd=workspace.path,
            json_schema=CLAUDE_STRUCTURED_SCHEMA,
            append_system_prompt=append_system_prompt,
        )
        if not output:
            return {"status": "FAILED", "summary": "Claude executor failed.", "notes": None}
        structured = _get_claude_structured(output)
        status = "DONE" if structured.get("status") == "DONE" else "FAILED"
        summary = structured.get("summary") if isinstance(structured.get("summary"), str) else None
        return {"status": status, "summary": summary, "notes": None, "raw": output}

    prompt = plan.get("claude_prompt") or json.dumps(plan, indent=2)
    prompt = (
        "PHASE: EXECUTE\n"
        "You are the executor. Implement the plan in this workspace.\n"
        "When finished, output JSON matching the executor_result schema:\n"
        '- Always include: status, questions, summary, notes. Use status "DONE" or "FAILED".\n'
        "- Set questions to [] (or null) unless status is NEEDS_REVIEWER.\n"
        "Do not include any extra keys.\n\n"
        f"Plan prompt:\n{prompt}\n"
    )
    output = codex_clients[executor.id].run_structured(
        prompt=prompt,
        schema_path=_executor_result_schema_path(),
        cwd=workspace.path,
    )
    return output


def run_multi_agent_session(
    *,
    task: Optional[str],
    config: Dict[str, Any],
    state_manager: StateManager,
    workspace_manager: WorkspaceManager,
    reviewers: List[AgentSpec],
    executors: List[AgentSpec],
    assignment: Dict[str, Any],
    repo_path: str,
    ui,
    telegram_client: Optional[TelegramClient],
    user_input_poll_interval_sec: float,
    user_input_timeout_sec: Optional[float],
    resuming: bool = False,
) -> Dict[str, Any]:
    session_mode = bool(config.get("orchestrator", {}).get("session_mode", False))
    max_iterations = _optional_positive_int(
        config.get("orchestrator", {}).get("max_iterations", 1),
        default=1,
    )
    max_reviewer_feedback_rounds = _optional_positive_int(
        config.get("orchestrator", {}).get("max_claude_question_rounds", 5),
        default=5,
    )
    branch_prefix = config.get("orchestrator", {}).get("branch_prefix", "luigi")
    try:
        branch_name_length = int(config.get("orchestrator", {}).get("branch_name_length", 8))
    except Exception:
        branch_name_length = 8
    try:
        branch_suffix_length = int(
            config.get("orchestrator", {}).get("branch_suffix_length", 6)
        )
    except Exception:
        branch_suffix_length = 6
    workspace_strategy = config.get("orchestrator", {}).get("workspace_strategy", "auto")
    use_git_worktree = config.get("orchestrator", {}).get("use_git_worktree", True)
    cleanup_policy = config.get("orchestrator", {}).get(
        "cleanup", "on_success"
    )  # always | on_success | never
    apply_changes_on_success = config.get("orchestrator", {}).get("apply_changes_on_success", True)
    commit_on_approval = config.get("orchestrator", {}).get("commit_on_approval", True)
    commit_message_template = config.get("orchestrator", {}).get("commit_message", "Task complete: {task}")
    auto_merge_on_approval = bool(config.get("orchestrator", {}).get("auto_merge_on_approval", False))
    merge_target_branch = config.get("orchestrator", {}).get("merge_target_branch", "main")
    merge_style = config.get("orchestrator", {}).get("merge_style", "merge_commit")
    dirty_main_policy = config.get("orchestrator", {}).get("dirty_main_policy", "commit")
    dirty_main_commit_message_template = config.get(
        "orchestrator",
        {},
    ).get(
        "dirty_main_commit_message",
        "Auto-commit local changes before Luigi merge (run {run_id})",
    )
    merge_commit_message_template = config.get(
        "orchestrator",
        {},
    ).get(
        "merge_commit_message",
        "Merge {branch} into {target} (run {run_id})",
    )
    delete_branch_on_merge = bool(config.get("orchestrator", {}).get("delete_branch_on_merge", True))
    delete_worktree_on_merge = bool(config.get("orchestrator", {}).get("delete_worktree_on_merge", True))
    carry_forward_between_iterations = bool(
        config.get("orchestrator", {}).get("carry_forward_workspace_between_iterations", True)
    )

    if len(executors) > 1 and workspace_strategy == "in_place":
        workspace_strategy = "auto"

    plan_schema = _load_schema(os.path.join(_schema_dir(), "codex_plan.schema.json"))
    decision_schema = _load_schema(_reviewer_decision_schema_path())
    executor_schema = _load_schema(_executor_result_schema_path())
    answer_schema = _load_schema(os.path.join(_schema_dir(), "reviewer_answer.schema.json"))

    codex_clients: Dict[str, CodexClient] = {}
    claude_clients: Dict[str, ClaudeCodeClient] = {}
    for reviewer in reviewers:
        if reviewer.kind == "codex":
            codex_clients[reviewer.id] = _build_codex_client_for_agent(reviewer, config["codex"], state_manager.log_dir)
        else:
            claude_clients[reviewer.id] = _build_claude_client_for_agent(reviewer, config["claude_code"], state_manager.log_dir)
    for executor in executors:
        if executor.kind == "codex":
            codex_clients[executor.id] = _build_codex_client_for_agent(executor, config["codex"], state_manager.log_dir)
        else:
            claude_clients[executor.id] = _build_claude_client_for_agent(executor, config["claude_code"], state_manager.log_dir)

    session_index = int(state_manager.get_state("session_index") or 0)
    original_repo_path = repo_path
    current_repo_path = repo_path
    final_cleanup_workspace = None
    final_approved = False
    final_persisted = False
    force_cleanup_worktree = False
    merge_branch_to_delete: Optional[str] = None
    delete_branch_after_merge = False
    merged_to_target_branch = False

    # Track per-agent runtime state for UI/monitoring.
    initial_runtime: Dict[str, Dict[str, Any]] = {}
    now_iso = datetime.now().isoformat()
    for agent in list(reviewers) + list(executors):
        initial_runtime[str(agent.id)] = {
            "id": str(agent.id),
            "kind": str(agent.kind),
            "role": str(agent.role),
            "status": "Stopped",
            "phase": "idle",
            "updated_at": now_iso,
        }
    state_manager.update_state("agent_runtime", initial_runtime)

    user_input_lock = threading.RLock()

    def _reviewer_answer_prompt(*, questions: list[str], context: Dict[str, Any], user_context: str) -> str:
        return (
            "PHASE: ANSWER_EXECUTOR\n"
            "You are a reviewer in a multi-agent system.\n"
            "An executor is implementing a plan and asked clarification questions.\n"
            "Answer concisely and with actionable guidance.\n"
            "- Always include ALL top-level fields: status, answer, questions, notes.\n"
            "- Use null or empty arrays for fields that do not apply.\n"
            "If you need clarification from the user to answer, do NOT guess. Output JSON with:\n"
            '  {"status":"NEEDS_USER_INPUT","questions":["..."]}\n'
            "Otherwise output JSON with:\n"
            '  {"status":"ANSWER","answer":"..."}\n'
            "Output MUST be valid JSON matching the provided schema.\n\n"
            f"Executor questions:\n{json.dumps(questions, indent=2)}\n\n"
            f"Context JSON:\n{json.dumps(context, indent=2)}\n\n"
            + (f"User context / answers:\n{user_context}\n" if user_context else "")
        )

    def _sync_global_agent_status_locked(runtime: Dict[str, Dict[str, Any]]) -> None:
        codex_phase = "idle"
        claude_phase = "idle"
        codex_running = False
        claude_running = False
        for info in runtime.values():
            if not isinstance(info, dict):
                continue
            kind = info.get("kind")
            status = info.get("status")
            phase = info.get("phase")
            if kind == "codex" and status == "Running":
                codex_running = True
                if codex_phase == "idle" and isinstance(phase, str) and phase:
                    codex_phase = phase
            if kind == "claude" and status == "Running":
                claude_running = True
                if claude_phase == "idle" and isinstance(phase, str) and phase:
                    claude_phase = phase
        state_manager.state["codex_status"] = "Running" if codex_running else "Stopped"
        state_manager.state["claude_status"] = "Running" if claude_running else "Stopped"
        state_manager.state["codex_phase"] = codex_phase if codex_running else "idle"
        state_manager.state["claude_phase"] = claude_phase if claude_running else "idle"

    def _set_agent_runtime(agent: AgentSpec, *, status: str, phase: str) -> None:
        ts = datetime.now().isoformat()
        with state_manager._lock:
            runtime = state_manager.state.get("agent_runtime")
            if not isinstance(runtime, dict):
                runtime = {}
            runtime = dict(runtime)
            info = runtime.get(agent.id)
            if not isinstance(info, dict):
                info = {}
            info = dict(info)
            info["id"] = str(agent.id)
            info["kind"] = str(agent.kind)
            info["role"] = str(agent.role)
            info["status"] = status
            info["phase"] = phase
            info["updated_at"] = ts
            if status == "Running":
                info["started_at"] = ts
            if status == "Stopped":
                info["stopped_at"] = ts
            runtime[str(agent.id)] = info
            state_manager.state["agent_runtime"] = runtime
            _sync_global_agent_status_locked(runtime)
            state_manager.save_state()

    def _run_with_agent_status(agent: AgentSpec, *, phase: str, fn):
        state_manager.add_to_history(f"{agent.role} {agent.id} ({agent.kind}) Running: {phase}")
        _set_agent_runtime(agent, status="Running", phase=phase)
        try:
            return fn()
        finally:
            _set_agent_runtime(agent, status="Stopped", phase="idle")
            state_manager.add_to_history(f"{agent.role} {agent.id} ({agent.kind}) Stopped")

    def _note(msg: str) -> None:
        print(msg)
        state_manager.add_to_history(msg)

    resume_state = state_manager.state if resuming else {}
    resume_stage = resume_state.get("stage") if isinstance(resume_state.get("stage"), str) else None
    resume_iteration = int(resume_state.get("iteration") or 0) if resuming else 0
    resume_plans = resume_state.get("plans") if isinstance(resume_state.get("plans"), dict) else None
    resume_candidates = (
        resume_state.get("candidates") if isinstance(resume_state.get("candidates"), dict) else None
    )
    resume_reviews = resume_state.get("reviews") if isinstance(resume_state.get("reviews"), dict) else None
    resume_used = False

    while True:
        if not task:
            task = _prompt_user_for_initial_task(
                state_manager=state_manager,
                ui_active=ui is not None and ui.is_running(),
                telegram=telegram_client,
                poll_interval_sec=user_input_poll_interval_sec,
                timeout_sec=user_input_timeout_sec,
            )
        session_index += 1
        state_manager.update_state("session_index", session_index)
        state_manager.update_state("task", task)
        state_manager.update_state("run_status", "running")
        _note(f"Session {session_index} started. Task: {task}")

        user_qna = state_manager.get_state("user_qna") or []
        if not isinstance(user_qna, list):
            user_qna = []

        def _ask_one_reviewer(
            reviewer: AgentSpec,
            *,
            questions: list[str],
            context: Dict[str, Any],
            cwd: str,
        ) -> Dict[str, Any]:
            """Ask a single reviewer to answer executor questions.

            Returns a dict matching `reviewer_answer.schema.json` (status ANSWER|NEEDS_USER_INPUT).
            """
            while True:
                if reviewer.kind == "codex":
                    answer = codex_clients[reviewer.id].answer_executor(
                        questions=questions,
                        context=context,
                        user_context=_format_user_context(user_qna),
                        cwd=cwd,
                    )
                else:
                    prompt = _reviewer_answer_prompt(
                        questions=questions,
                        context=context,
                        user_context=_format_user_context(user_qna),
                    )
                    answer = claude_clients[reviewer.id].run_structured(
                        prompt=prompt,
                        json_schema=answer_schema,
                        cwd=cwd,
                        allowed_tools_override=["Read", "Glob", "Grep"],
                    )
                    if not answer:
                        raise RuntimeError("Claude reviewer failed to answer executor questions.")

                if answer.get("status") != "NEEDS_USER_INPUT":
                    return answer

                user_questions = answer.get("questions", [])
                if not isinstance(user_questions, list) or not user_questions:
                    raise RuntimeError("Reviewer returned NEEDS_USER_INPUT without questions.")

                with user_input_lock:
                    prev_stage = state_manager.get_state("stage")
                    new_qna = _prompt_user_for_answers(
                        [str(q) for q in user_questions],
                        state_manager=state_manager,
                        ui_active=ui is not None and ui.is_running(),
                        poll_interval_sec=user_input_poll_interval_sec,
                        timeout_sec=user_input_timeout_sec,
                    )
                    user_qna.extend(new_qna)
                    state_manager.update_state("user_qna", user_qna)
                    if isinstance(prev_stage, str) and prev_stage:
                        state_manager.update_state("stage", prev_stage)

        def _ask_reviewers(
            *,
            questions: list[str],
            context: Dict[str, Any],
            cwd: str,
            phase_prefix: str,
            reviewers_to_ask: List[AgentSpec],
        ) -> str:
            """Ask reviewers and return a merged answer text."""
            answers: list[str] = []
            for reviewer in reviewers_to_ask:
                ans_obj = _run_with_agent_status(
                    reviewer,
                    phase=f"{phase_prefix}:{reviewer.id}",
                    fn=lambda r=reviewer: _ask_one_reviewer(
                        r,
                        questions=questions,
                        context=context,
                        cwd=cwd,
                    ),
                )
                answer_text = str(ans_obj.get("answer") or "").strip()
                if not answer_text:
                    answer_text = "(empty answer)"
                answers.append(f"[{reviewer.id} / {reviewer.kind}] {answer_text}")
            return "\n\n".join(answers)

        approved = False
        persisted = False
        # Per-session cleanup flags (main() cleanup doesn't run while session_mode loops here).
        force_cleanup_worktree = False
        merge_branch_to_delete = None
        delete_branch_after_merge = False
        merged_to_target_branch = False
        iteration = 0
        final_selected_candidate = None
        final_workspace = None
        state_manager.update_state("approved", False)

        while not approved:
            is_resume_iteration = resuming and not resume_used and resume_iteration > 0

            next_iteration = resume_iteration if is_resume_iteration else (iteration + 1)
            if max_iterations is not None and next_iteration > max_iterations:
                # Iteration limit reached. Ask admin whether to accept partial output or extend.
                ui_active = ui is not None and ui.is_running()
                if not ui_active and not telegram_client:
                    break

                # "task" may have been overwritten with the next-iteration prompt in rejected runs.
                missing_summary = str(task or "").strip()
                if not missing_summary:
                    reviews_obj = state_manager.get_state("reviews")
                    if isinstance(reviews_obj, dict):
                        parts: list[str] = []
                        for rid, d in reviews_obj.items():
                            if not isinstance(d, dict):
                                continue
                            nxt = str(d.get("next_prompt") or "").strip()
                            if nxt:
                                parts.append(f"[{rid}] {nxt}")
                        missing_summary = "\n\n".join(parts).strip()

                if telegram_client and missing_summary:
                    _send_telegram_message(
                        state_manager=state_manager,
                        telegram=telegram_client,
                        text=(
                            f"Max iterations reached (iteration {iteration} / {max_iterations}).\n\n"
                            "Summary of remaining work (from reviewers/next prompt):\n"
                            f"{missing_summary}"
                        ),
                        label="max_iterations_summary",
                    )

                preview = _preview_one_line(missing_summary, max_len=160) or "(no summary available)"
                extend_by = 5
                options = [
                    {
                        "label": f"Stop now and accept partial result (missing: {preview})",
                        "action": "accept_partial",
                        "missing_summary": missing_summary,
                        "iteration": iteration,
                        "max_iterations": max_iterations,
                    },
                    {
                        "label": f"Continue for {extend_by} more iterations",
                        "action": "extend",
                        "extend_by": extend_by,
                        "iteration": iteration,
                        "max_iterations": max_iterations,
                    },
                ]
                admin_choice = _await_admin_decision(
                    state_manager=state_manager,
                    options=options,
                    ui_active=ui_active,
                    telegram=telegram_client,
                    poll_interval_sec=user_input_poll_interval_sec,
                    timeout_sec=user_input_timeout_sec,
                )
                choice_idx = int(admin_choice.get("choice") or 1) - 1
                choice_idx = max(0, min(choice_idx, len(options) - 1))
                selection = options[choice_idx]
                if selection.get("action") == "extend":
                    max_iterations = int(max_iterations) + int(selection.get("extend_by") or extend_by)
                    _note(f"Admin extended max_iterations to {max_iterations}.")
                    state_manager.add_to_history(f"Admin extended max_iterations to {max_iterations}.")
                    continue

                # Accept partial: mark approved and persist the current best workspace.
                state_manager.add_to_history(
                    "Admin accepted partial result after reaching max iterations."
                )
                state_manager.update_state("max_iterations_missing_summary", missing_summary)
                state_manager.update_state("approved_by_admin", True)
                approved = True
                state_manager.update_state("approved", True)

                selected_workspace = final_workspace
                if not selected_workspace and final_selected_candidate:
                    try:
                        selected_workspace = candidate_workspaces.get(final_selected_candidate.get("id"))
                    except Exception:
                        selected_workspace = None

                if selected_workspace:
                    try:
                        if selected_workspace.strategy == "worktree" and commit_on_approval:
                            # Prefer the original user task for commit messages; `task` may have been overwritten.
                            commit_task = state_manager.get_state("task")
                            commit_message = commit_message_template.format(
                                task=commit_task or task, run_id=state_manager.run_id
                            )
                            commit_sha = selected_workspace.commit_changes(commit_message)
                            state_manager.update_state("commit_sha", commit_sha)
                            state_manager.update_state("branch_name", selected_workspace.branch_name)
                            if selected_workspace.branch_name:
                                print(f"Committed to branch: {selected_workspace.branch_name}")
                            if commit_sha:
                                print(f"Commit: {commit_sha}")
                            if auto_merge_on_approval:
                                merge_branch = selected_workspace.branch_name
                                merge_message = merge_commit_message_template.format(
                                    task=str(state_manager.get_state("task") or ""),
                                    run_id=state_manager.run_id,
                                    branch=merge_branch or "",
                                    target=merge_target_branch,
                                )
                                dirty_message_template = dirty_main_commit_message_template
                                merge_client = _pick_merge_claude_client(
                                    claude_clients,
                                    preferred_id=final_selected_candidate.get("executor_id")
                                    if isinstance(final_selected_candidate, dict)
                                    else None,
                                )
                                selected_plan = None
                                if isinstance(final_selected_candidate, dict):
                                    selected_plan = reviewer_plans.get(
                                        final_selected_candidate.get("reviewer_id")
                                    )
                                state_manager.update_state("merge_branch", merge_branch)
                                state_manager.update_state("merge_target_branch", merge_target_branch)
                                state_manager.update_state("stage", "merging")
                                state_manager.update_state("merge_status", "running")
                                merge_result = _auto_merge_worktree_branch(
                                    repo_path=original_repo_path,
                                    branch_name=merge_branch,
                                    target_branch=merge_target_branch,
                                    merge_style=merge_style,
                                    dirty_main_policy=dirty_main_policy,
                                    dirty_main_commit_message_template=dirty_message_template,
                                    merge_commit_message=merge_message,
                                    claude_client=merge_client,
                                    task=str(state_manager.get_state("task") or task or ""),
                                    run_id=state_manager.run_id,
                                    plan=selected_plan,
                                    reviewer_decisions=reviewer_decisions,
                                    candidate=final_selected_candidate,
                                    note_fn=_note,
                                )
                                state_manager.update_state(
                                    "merge_status",
                                    "merged" if merge_result.get("merged") else "failed",
                                )
                                state_manager.update_state(
                                    "merge_commit_sha", merge_result.get("merge_commit_sha")
                                )
                                state_manager.update_state(
                                    "dirty_main_commit_sha",
                                    merge_result.get("dirty_main_commit_sha"),
                                )
                                if merge_result.get("conflict_files"):
                                    state_manager.update_state(
                                        "merge_conflict_files", merge_result.get("conflict_files")
                                    )
                                if merge_result.get("claude_merge_summary"):
                                    state_manager.update_state(
                                        "merge_resolution_summary",
                                        merge_result.get("claude_merge_summary"),
                                    )
                                if merge_result.get("merged"):
                                    persisted = True
                                    merged_to_target_branch = True
                                    if delete_worktree_on_merge:
                                        force_cleanup_worktree = True
                                    if delete_branch_on_merge and merge_branch:
                                        delete_branch_after_merge = True
                                        merge_branch_to_delete = merge_branch
                                else:
                                    persisted = False
                                    err = merge_result.get("error") or "unknown merge error"
                                    state_manager.add_to_history(f"Auto-merge failed: {err}")
                                    state_manager.update_state("merge_error", err)
                                    print(f"Auto-merge failed: {err}")
                            else:
                                persisted = True
                        elif selected_workspace.strategy == "copy" and apply_changes_on_success:
                            selected_workspace.apply_to_repo()
                            persisted = True
                            print(f"Applied changes to repo: {repo_path}")
                        else:
                            persisted = True
                    except Exception as e:
                        persisted = False
                        state_manager.add_to_history(f"Persistence step failed: {e}")
                        print(f"Persistence step failed: {e}")
                    state_manager.update_state("persisted", persisted)
                    state_manager.update_state(
                        "stage", "complete" if persisted else "persistence_failed"
                    )
                else:
                    persisted = False
                    state_manager.add_to_history(
                        "Admin requested partial acceptance, but no selected workspace was available to persist."
                    )
                    state_manager.update_state("persisted", False)

                break

            # Start the next iteration
            iteration = next_iteration
            state_manager.update_state("iteration", iteration)

            reviewer_plans: Dict[str, Dict[str, Any]] = {}
            use_resume_plans = (
                is_resume_iteration
                and resume_plans
                and resume_stage in ("plan_ready", "executing", "tests_ready", "reviewing", "review_ready")
            )
            if use_resume_plans:
                reviewer_plans = resume_plans
                state_manager.update_state("plans", reviewer_plans)
                state_manager.update_state("stage", "plan_ready")
                _note(f"Iteration {iteration}: resuming from existing plans.")
            else:
                state_manager.update_state("stage", "planning")
                _note(f"Iteration {iteration}: planning with {len(reviewers)} reviewers...")

                def _plan_one(reviewer: AgentSpec) -> Dict[str, Any]:
                    return _run_with_agent_status(
                        reviewer,
                        phase="plan",
                        fn=lambda: _run_reviewer_plan(
                            reviewer,
                            codex_clients=codex_clients,
                            claude_clients=claude_clients,
                            task=task or "",
                            user_context=_format_user_context(user_qna),
                            cwd=current_repo_path,
                            plan_schema=plan_schema,
                        ),
                    )

                with ThreadPoolExecutor(max_workers=len(reviewers)) as pool:
                    futures = {pool.submit(_plan_one, reviewer): reviewer for reviewer in reviewers}
                    for future in as_completed(futures):
                        reviewer = futures[future]
                        try:
                            reviewer_plans[reviewer.id] = future.result()
                        except Exception as exc:
                            state_manager.add_to_history(f"Reviewer {reviewer.id} plan failed: {exc}")
                _note("Plans created.")

                # Validate plans; drop invalid ones so we don't generate broken candidates.
                plan_errors: Dict[str, Any] = {}
                validated_plans: Dict[str, Dict[str, Any]] = {}
                for reviewer in reviewers:
                    raw_plan = reviewer_plans.get(reviewer.id)
                    try:
                        validated_plans[reviewer.id] = _validate_reviewer_plan(raw_plan or {})
                    except Exception as exc:
                        plan_errors[reviewer.id] = {"error": str(exc), "raw": raw_plan}
                        state_manager.add_to_history(f"Reviewer {reviewer.id} plan invalid: {exc}")

                reviewer_plans = validated_plans
                if plan_errors:
                    state_manager.update_state("plan_errors", plan_errors)

                # Handle reviewer questions serially if any
                for reviewer in reviewers:
                    plan = reviewer_plans.get(reviewer.id) or {}
                    while plan.get("status") == "NEEDS_USER_INPUT":
                        _note(f"Reviewer {reviewer.id} requested clarification questions.")
                        questions = plan.get("questions", [])
                        new_qna = _prompt_user_for_answers(
                            [str(q) for q in questions],
                            state_manager=state_manager,
                            ui_active=ui is not None and ui.is_running(),
                            poll_interval_sec=user_input_poll_interval_sec,
                            timeout_sec=user_input_timeout_sec,
                        )
                        user_qna.extend(new_qna)
                        state_manager.update_state("user_qna", user_qna)
                        plan = _run_with_agent_status(
                            reviewer,
                            phase="plan_followup",
                            fn=lambda: _run_reviewer_plan(
                                reviewer,
                                codex_clients=codex_clients,
                                claude_clients=claude_clients,
                                task=task or "",
                                user_context=_format_user_context(user_qna),
                                cwd=current_repo_path,
                                plan_schema=plan_schema,
                            ),
                        )
                        reviewer_plans[reviewer.id] = plan

                state_manager.update_state("plans", reviewer_plans)
                state_manager.update_state("stage", "plan_ready")

            reviewer_ids = list(reviewer_plans.keys())
            assignments = _assign_executors(
                reviewer_ids,
                executors,
                executors_per_plan=int(assignment.get("executors_per_plan", 1)),
            )
            candidate_strategy = workspace_strategy
            if carry_forward_between_iterations and current_repo_path != original_repo_path:
                # Carry-forward includes uncommitted file changes; only the copy strategy can safely
                # incorporate those as the next iteration's baseline while still applying back to
                # the original repo on approval.
                candidate_strategy = "copy"
            if len(assignments) > 1 and candidate_strategy == "in_place":
                # Running multiple candidates concurrently in the same directory would corrupt the workspace.
                candidate_strategy = "auto"

            candidates: Dict[str, Dict[str, Any]] = {}
            candidate_workspaces: Dict[str, Workspace] = {}
            use_resume_candidates = is_resume_iteration and bool(resume_candidates)
            if use_resume_candidates:
                candidates = resume_candidates
                _note(f"Iteration {iteration}: resuming from {len(candidates)} existing candidates.")
                for cid, cand in candidates.items():
                    if not isinstance(cand, dict):
                        continue
                    strategy = cand.get("workspace_strategy")
                    workspace_path = cand.get("workspace_path")
                    ws = workspace_manager.resume_candidate(
                        repo_path=original_repo_path,
                        run_id=state_manager.run_id,
                        iteration=iteration,
                        candidate_id=cid,
                        workspace_path=workspace_path,
                        workspace_strategy=strategy,
                    )
                    if not ws:
                        # Recreate the candidate workspace if we cannot resume it.
                        ws = workspace_manager.create_candidate(
                            repo_path=original_repo_path,
                            source_path=current_repo_path,
                            run_id=state_manager.run_id,
                            iteration=iteration,
                            candidate_id=cid,
                            strategy=strategy or candidate_strategy,
                            use_git_worktree=use_git_worktree,
                            branch_prefix=branch_prefix,
                            branch_name_length=branch_name_length,
                            branch_suffix_length=branch_suffix_length,
                        )
                        cand["workspace_path"] = ws.path
                        cand["workspace_strategy"] = ws.strategy
                    candidate_workspaces[cid] = ws
                state_manager.update_state("candidates", candidates)
            else:
                for idx, assign in enumerate(assignments):
                    reviewer_id = assign["reviewer_id"]
                    executor = assign["executor"]
                    candidate_id = f"iter{iteration}-{reviewer_id}-{executor.id}-{idx+1}"
                    workspace = workspace_manager.create_candidate(
                        repo_path=original_repo_path,
                        source_path=current_repo_path,
                        run_id=state_manager.run_id,
                        iteration=iteration,
                        candidate_id=candidate_id,
                        strategy=candidate_strategy,
                        use_git_worktree=use_git_worktree,
                        branch_prefix=branch_prefix,
                        branch_name_length=branch_name_length,
                        branch_suffix_length=branch_suffix_length,
                    )
                    candidate_workspaces[candidate_id] = workspace
                    candidates[candidate_id] = {
                        "id": candidate_id,
                        "reviewer_id": reviewer_id,
                        "executor_id": executor.id,
                        "workspace_path": workspace.path,
                        "workspace_strategy": workspace.strategy,
                        "status": "PENDING",
                    }

                state_manager.update_state("candidates", candidates)
                _note(f"Iteration {iteration}: created {len(candidates)} candidates.")

            pending_ids = [
                cid
                for cid, cand in candidates.items()
                if isinstance(cand, dict) and cand.get("status") not in ("DONE", "FAILED")
            ]
            if pending_ids:
                state_manager.update_state("stage", "executing")
                _note(f"Iteration {iteration}: executing {len(pending_ids)} pending candidates...")

                # Persist a RUNNING status for live UI feedback.
                for cid in pending_ids:
                    candidates[cid]["status"] = "RUNNING"
                state_manager.update_state("candidates", candidates)
            else:
                _note(f"Iteration {iteration}: all candidates already completed; skipping execution.")

            def _execute_candidate(candidate_id: str) -> Dict[str, Any]:
                candidate = dict(candidates[candidate_id])
                plan = reviewer_plans[candidate["reviewer_id"]]
                executor = next(e for e in executors if e.id == candidate["executor_id"])
                workspace = candidate_workspaces[candidate_id]
                reviewers_to_ask = [r for r in reviewers if r.id in reviewer_plans] or list(reviewers)

                def _run_executor_with_reviewer_feedback() -> Dict[str, Any]:
                    base_context: Dict[str, Any] = {
                        "task": task,
                        "iteration": iteration,
                        "candidate_id": candidate_id,
                        "reviewer_id": candidate.get("reviewer_id"),
                        "executor_id": executor.id,
                        "workspace_path": workspace.path,
                    }

                    feedback_round = 0

                    if executor.kind == "claude":
                        session_id = None
                        prompt: Any = plan
                        while True:
                            output = claude_clients[executor.id].implement(
                                prompt,
                                session_id=session_id,
                                cwd=workspace.path,
                                json_schema=CLAUDE_STRUCTURED_SCHEMA,
                                append_system_prompt=CLAUDE_APPEND_SYSTEM_PROMPT,
                            )
                            if not output:
                                return {"status": "FAILED", "summary": "Claude executor failed.", "notes": None}

                            session_id = output.get("session_id") or session_id
                            structured = _get_claude_structured(output)
                            status = structured.get("status")
                            summary = structured.get("summary") if isinstance(structured.get("summary"), str) else None

                            if status == "DONE":
                                return {
                                    "status": "DONE",
                                    "summary": summary,
                                    "notes": None,
                                    "raw": output,
                                }

                            if status in ("NEEDS_REVIEWER", "NEEDS_CODEX"):
                                questions = structured.get("questions", [])
                                if not isinstance(questions, list) or not questions:
                                    return {
                                        "status": "FAILED",
                                        "summary": "Executor requested reviewer input without questions.",
                                        "notes": None,
                                        "raw": output,
                                    }

                                feedback_round += 1
                                if (
                                    max_reviewer_feedback_rounds is not None
                                    and feedback_round > max_reviewer_feedback_rounds
                                ):
                                    return {
                                        "status": "FAILED",
                                        "summary": "Executor exceeded max reviewer feedback rounds.",
                                        "notes": None,
                                        "raw": output,
                                    }

                                state_manager.add_to_history(
                                    f"Executor {executor.id} requested reviewer feedback (round {feedback_round})."
                                )
                                ctx = dict(base_context)
                                ctx["executor_summary"] = summary
                                reviewers_text = _ask_reviewers(
                                    questions=[str(q) for q in questions],
                                    context=ctx,
                                    cwd=workspace.path,
                                    phase_prefix=f"answer_executor:{candidate_id}:r{feedback_round}",
                                    reviewers_to_ask=reviewers_to_ask,
                                )
                                prompt = (
                                    "Continue implementing the plan.\n\n"
                                    "Here are answers from the reviewers to your questions:\n"
                                    f"{reviewers_text}\n"
                                )
                                continue

                            # Unknown/FAILED status: stop and surface summary.
                            return {
                                "status": "FAILED",
                                "summary": summary or f"Executor returned status {status!r}.",
                                "notes": None,
                                "raw": output,
                            }

                    # Codex executor
                    plan_prompt = plan.get("claude_prompt") or json.dumps(plan, indent=2)
                    prompt = (
                        "PHASE: EXECUTE\n"
                        "You are the executor. Implement the plan in this workspace.\n"
                        "If you need clarification from reviewers, output JSON with status NEEDS_REVIEWER and a non-empty questions array.\n"
                        "When finished, output JSON matching the executor_result schema.\n"
                        '- Always include: status, questions, summary, notes. Use status "DONE", "FAILED", or "NEEDS_REVIEWER".\n'
                        "- Set questions to [] (or null) unless status is NEEDS_REVIEWER.\n"
                        "Do not include any extra keys.\n\n"
                        f"Plan prompt:\n{plan_prompt}\n"
                    )
                    while True:
                        output = codex_clients[executor.id].run_structured(
                            prompt=prompt,
                            schema_path=_executor_result_schema_path(),
                            cwd=workspace.path,
                        )
                        status = output.get("status")
                        if status in ("DONE", "FAILED"):
                            return output

                        if status != "NEEDS_REVIEWER":
                            return {
                                "status": "FAILED",
                                "summary": f"Codex executor returned unexpected status {status!r}.",
                                "notes": None,
                            }

                        questions = output.get("questions", [])
                        if not isinstance(questions, list) or not questions:
                            return {
                                "status": "FAILED",
                                "summary": "Codex executor requested reviewer input without questions.",
                                "notes": None,
                            }

                        feedback_round += 1
                        if (
                            max_reviewer_feedback_rounds is not None
                            and feedback_round > max_reviewer_feedback_rounds
                        ):
                            return {
                                "status": "FAILED",
                                "summary": "Executor exceeded max reviewer feedback rounds.",
                                "notes": None,
                            }

                        state_manager.add_to_history(
                            f"Executor {executor.id} requested reviewer feedback (round {feedback_round})."
                        )
                        ctx = dict(base_context)
                        ctx["executor_summary"] = output.get("summary")
                        reviewers_text = _ask_reviewers(
                            questions=[str(q) for q in questions],
                            context=ctx,
                            cwd=workspace.path,
                            phase_prefix=f"answer_executor:{candidate_id}:r{feedback_round}",
                            reviewers_to_ask=reviewers_to_ask,
                        )
                        prompt = (
                            f"{prompt}\n\n"
                            "Continue implementing the plan.\n\n"
                            f"Reviewer answers (round {feedback_round}):\n{reviewers_text}\n"
                        )

                exec_output = _run_with_agent_status(
                    executor,
                    phase=f"execute:{candidate_id}",
                    fn=_run_executor_with_reviewer_feedback,
                )
                candidate["executor_output"] = exec_output
                candidate["executor_summary"] = exec_output.get("summary") if isinstance(exec_output, dict) else None
                candidate["status"] = "DONE" if exec_output.get("status") == "DONE" else "FAILED"
                plan_test_commands = plan.get("test_commands") if isinstance(plan, dict) else None
                test_results = run_tests(cwd=workspace.path, config=config, test_commands=plan_test_commands)
                candidate["test_results"] = test_results
                candidate["test_summary"] = _summarize_test_results(test_results)
                diff = workspace.get_diff()
                candidate["diff"] = diff
                candidate["diff_preview"] = "\n".join(diff.splitlines()[:40]) if diff else ""
                return candidate

            if pending_ids:
                with ThreadPoolExecutor(max_workers=len(pending_ids)) as pool:
                    futures = {pool.submit(_execute_candidate, cid): cid for cid in pending_ids}
                    for future in as_completed(futures):
                        cid = futures[future]
                        try:
                            candidates[cid] = future.result()
                        except Exception as exc:
                            candidates[cid] = dict(candidates[cid])
                            candidates[cid]["status"] = "FAILED"
                            candidates[cid]["error"] = str(exc)
                            state_manager.add_to_history(f"Candidate {cid} crashed: {exc}")
                        state_manager.update_state("candidates", candidates)
                        _note(f"Candidate {cid} finished: {candidates[cid].get('status')}")

                state_manager.update_state("stage", "tests_ready")
                _note("All candidates finished.")
            else:
                if resume_stage:
                    state_manager.update_state("stage", resume_stage)
                else:
                    state_manager.update_state("stage", "tests_ready")

            candidates_text = "\n\n".join(_candidate_summary_text(c) for c in candidates.values())
            use_resume_reviews = (
                is_resume_iteration and resume_reviews and resume_stage == "review_ready"
            )
            if use_resume_reviews:
                reviewer_decisions = resume_reviews
                _note(f"Iteration {iteration}: resuming from existing reviewer decisions.")
            else:
                state_manager.update_state("stage", "reviewing")
                _note(f"Iteration {iteration}: reviewers evaluating candidates...")

            if not use_resume_reviews:
                reviewer_decisions: Dict[str, Dict[str, Any]] = {}
                decision_prompt = _review_candidates_prompt(
                    task=task,
                    candidates_text=candidates_text,
                    user_context=_format_user_context(user_qna),
                    final_handoff=False,
                )

                def _decide_one(reviewer: AgentSpec) -> Dict[str, Any]:
                    return _run_with_agent_status(
                        reviewer,
                        phase="review_candidates",
                        fn=lambda: _run_reviewer_decision(
                            reviewer,
                            codex_clients=codex_clients,
                            claude_clients=claude_clients,
                            prompt=decision_prompt,
                            cwd=current_repo_path,
                            decision_schema=decision_schema,
                        ),
                    )

                with ThreadPoolExecutor(max_workers=len(reviewers)) as pool:
                    futures = {pool.submit(_decide_one, reviewer): reviewer for reviewer in reviewers}
                    for future in as_completed(futures):
                        reviewer = futures[future]
                        try:
                            reviewer_decisions[reviewer.id] = future.result()
                        except Exception as exc:
                            reviewer_decisions[reviewer.id] = None
                            state_manager.add_to_history(f"Reviewer {reviewer.id} decision failed: {exc}")
                _note("Reviewer decisions received.")

                for reviewer in reviewers:
                    decision = reviewer_decisions.get(reviewer.id) or {}
                    while decision.get("status") == "NEEDS_USER_INPUT":
                        _note(f"Reviewer {reviewer.id} requested clarification before deciding.")
                        questions = decision.get("questions", [])
                        if not isinstance(questions, list) or not questions:
                            raise RuntimeError(
                                "Reviewer returned NEEDS_USER_INPUT without questions."
                            )
                        new_qna = _prompt_user_for_answers(
                            [str(q) for q in questions],
                            state_manager=state_manager,
                            ui_active=ui is not None and ui.is_running(),
                            poll_interval_sec=user_input_poll_interval_sec,
                            timeout_sec=user_input_timeout_sec,
                        )
                        user_qna.extend(new_qna)
                        state_manager.update_state("user_qna", user_qna)
                        decision = _run_with_agent_status(
                            reviewer,
                            phase="review_candidates_followup",
                            fn=lambda: _run_reviewer_decision(
                                reviewer,
                                codex_clients=codex_clients,
                                claude_clients=claude_clients,
                                prompt=_review_candidates_prompt(
                                    task=task,
                                    candidates_text=candidates_text,
                                    user_context=_format_user_context(user_qna),
                                    final_handoff=False,
                                ),
                                cwd=current_repo_path,
                                decision_schema=decision_schema,
                            ),
                        )
                        reviewer_decisions[reviewer.id] = decision

            candidate_ids = set(candidates.keys())
            validated_decisions: Dict[str, Dict[str, Any]] = {}
            decision_errors: Dict[str, Any] = {}
            for reviewer in reviewers:
                raw = reviewer_decisions.get(reviewer.id)
                try:
                    validated_decisions[reviewer.id] = _validate_reviewer_decision(raw or {}, candidate_ids)
                except Exception as exc:
                    decision_errors[reviewer.id] = {"error": str(exc), "raw": raw}
                    state_manager.add_to_history(f"Reviewer {reviewer.id} decision invalid: {exc}")

            if decision_errors:
                state_manager.update_state("review_errors", decision_errors)

            reviewer_decisions = validated_decisions
            if not reviewer_decisions:
                # If all reviewers failed to produce a valid decision, ask the admin to pick a candidate
                # to carry forward. Default behavior is REJECT (no persistence) for safety.
                state_manager.add_to_history("All reviewer decisions invalid; awaiting admin candidate choice.")
                options = []
                for cand in candidates.values():
                    options.append(
                        {
                            "label": f"{cand.get('id')}: status={cand.get('status')} tests={cand.get('test_summary')}",
                            "candidate_id": cand.get("id"),
                        }
                    )
                admin_choice = _await_admin_decision(
                    state_manager=state_manager,
                    options=options,
                    ui_active=ui is not None and ui.is_running(),
                    telegram=telegram_client,
                    poll_interval_sec=user_input_poll_interval_sec,
                    timeout_sec=user_input_timeout_sec,
                )
                choice_idx = int(admin_choice.get("choice") or 1) - 1
                choice_idx = max(0, min(choice_idx, len(options) - 1))
                winner = options[choice_idx].get("candidate_id")
                next_prompt = task
                if admin_choice.get("notes"):
                    note = admin_choice.get("notes")
                    next_prompt = f"{next_prompt}\n\nAdmin notes:\n{note}"
                reviewer_decisions = {
                    "admin": {
                        "status": "REJECTED",
                        "winner_candidate_id": winner,
                        "summary": "Admin selected a candidate because reviewers returned invalid decisions.",
                        "feedback": "Fix reviewer decision generation/validation and rerun.",
                        "next_prompt": next_prompt,
                        "questions": None,
                        "notes": admin_choice.get("notes", ""),
                    }
                }

            state_manager.update_state("reviews", reviewer_decisions)
            state_manager.update_state("stage", "review_ready")

            consensus_result = _compute_consensus(reviewer_decisions)
            winner = consensus_result.get("winner")
            next_prompt = consensus_result.get("next_prompt")
            status = consensus_result.get("status")
            consensus = bool(consensus_result.get("consensus"))

            if not consensus:
                options = []
                for reviewer_id, decision in reviewer_decisions.items():
                    options.append(
                        {
                            "label": f"{reviewer_id}: winner={decision.get('winner_candidate_id')} status={decision.get('status')} next={decision.get('next_prompt')}",
                            "reviewer_id": reviewer_id,
                            "decision": decision,
                        }
                    )
                admin_choice = _await_admin_decision(
                    state_manager=state_manager,
                    options=options,
                    ui_active=ui is not None and ui.is_running(),
                    telegram=telegram_client,
                    poll_interval_sec=user_input_poll_interval_sec,
                    timeout_sec=user_input_timeout_sec,
                )
                choice_idx = int(admin_choice.get("choice") or 1) - 1
                choice_idx = max(0, min(choice_idx, len(options) - 1))
                decision = options[choice_idx]["decision"]
                winner = decision.get("winner_candidate_id")
                next_prompt = decision.get("next_prompt")
                status = decision.get("status")
                if admin_choice.get("notes"):
                    note = admin_choice.get("notes")
                    next_prompt = f"{next_prompt}\n\nAdmin notes:\n{note}" if next_prompt else f"Admin notes:\n{note}"

            final_selected_candidate = candidates.get(winner) if winner else None
            if not final_selected_candidate:
                raise RuntimeError("Reviewer decision did not select a valid candidate.")
            if status == "APPROVED":
                approved = True
            else:
                task = next_prompt or task
            state_manager.update_state("approved", approved)
            if is_resume_iteration:
                resume_used = True

            # Cleanup non-selected candidates
            for cid, cand in candidates.items():
                if final_selected_candidate and cid == final_selected_candidate.get("id"):
                    continue
                # Best-effort cleanup for worktrees/copies
                if cand.get("workspace_path"):
                    try:
                        candidate_workspaces[cid].cleanup()
                    except Exception:
                        pass

            # Promote selected candidate
            if final_selected_candidate:
                selected_workspace = candidate_workspaces.get(final_selected_candidate.get("id"))
                final_workspace = selected_workspace
                if approved and selected_workspace:
                    try:
                        if selected_workspace.strategy == "worktree" and commit_on_approval:
                            commit_message = commit_message_template.format(
                                task=task, run_id=state_manager.run_id
                            )
                            commit_sha = selected_workspace.commit_changes(commit_message)
                            state_manager.update_state("commit_sha", commit_sha)
                            state_manager.update_state("branch_name", selected_workspace.branch_name)
                            if selected_workspace.branch_name:
                                print(f"Committed to branch: {selected_workspace.branch_name}")
                            if commit_sha:
                                print(f"Commit: {commit_sha}")
                            if auto_merge_on_approval:
                                merge_branch = selected_workspace.branch_name
                                merge_message = merge_commit_message_template.format(
                                    task=task or "",
                                    run_id=state_manager.run_id,
                                    branch=merge_branch or "",
                                    target=merge_target_branch,
                                )
                                dirty_message_template = dirty_main_commit_message_template
                                merge_client = _pick_merge_claude_client(
                                    claude_clients,
                                    preferred_id=final_selected_candidate.get("executor_id")
                                    if isinstance(final_selected_candidate, dict)
                                    else None,
                                )
                                selected_plan = None
                                if isinstance(final_selected_candidate, dict):
                                    selected_plan = reviewer_plans.get(
                                        final_selected_candidate.get("reviewer_id")
                                    )
                                state_manager.update_state("merge_branch", merge_branch)
                                state_manager.update_state("merge_target_branch", merge_target_branch)
                                state_manager.update_state("stage", "merging")
                                state_manager.update_state("merge_status", "running")
                                merge_result = _auto_merge_worktree_branch(
                                    repo_path=original_repo_path,
                                    branch_name=merge_branch,
                                    target_branch=merge_target_branch,
                                    merge_style=merge_style,
                                    dirty_main_policy=dirty_main_policy,
                                    dirty_main_commit_message_template=dirty_message_template,
                                    merge_commit_message=merge_message,
                                    claude_client=merge_client,
                                    task=task,
                                    run_id=state_manager.run_id,
                                    plan=selected_plan,
                                    reviewer_decisions=reviewer_decisions,
                                    candidate=final_selected_candidate,
                                    note_fn=_note,
                                )
                                state_manager.update_state(
                                    "merge_status",
                                    "merged" if merge_result.get("merged") else "failed",
                                )
                                state_manager.update_state(
                                    "merge_commit_sha", merge_result.get("merge_commit_sha")
                                )
                                state_manager.update_state(
                                    "dirty_main_commit_sha",
                                    merge_result.get("dirty_main_commit_sha"),
                                )
                                if merge_result.get("conflict_files"):
                                    state_manager.update_state(
                                        "merge_conflict_files", merge_result.get("conflict_files")
                                    )
                                if merge_result.get("claude_merge_summary"):
                                    state_manager.update_state(
                                        "merge_resolution_summary",
                                        merge_result.get("claude_merge_summary"),
                                    )
                                if merge_result.get("merged"):
                                    persisted = True
                                    merged_to_target_branch = True
                                    if delete_worktree_on_merge:
                                        force_cleanup_worktree = True
                                    if delete_branch_on_merge and merge_branch:
                                        delete_branch_after_merge = True
                                        merge_branch_to_delete = merge_branch
                                else:
                                    persisted = False
                                    err = merge_result.get("error") or "unknown merge error"
                                    state_manager.add_to_history(f"Auto-merge failed: {err}")
                                    state_manager.update_state("merge_error", err)
                                    print(f"Auto-merge failed: {err}")
                            else:
                                persisted = True
                        elif selected_workspace.strategy == "copy" and apply_changes_on_success:
                            selected_workspace.apply_to_repo()
                            persisted = True
                            print(f"Applied changes to repo: {repo_path}")
                        else:
                            persisted = True
                    except Exception as e:
                        persisted = False
                        state_manager.add_to_history(f"Persistence step failed: {e}")
                        print(f"Persistence step failed: {e}")
                    state_manager.update_state("persisted", persisted)
                    state_manager.update_state(
                        "stage", "complete" if persisted else "persistence_failed"
                    )
                if selected_workspace:
                    if approved:
                        if selected_workspace.strategy == "copy" and apply_changes_on_success:
                            current_repo_path = repo_path
                        elif (
                            selected_workspace.strategy == "worktree"
                            and auto_merge_on_approval
                            and merged_to_target_branch
                        ):
                            current_repo_path = repo_path
                        else:
                            current_repo_path = selected_workspace.path
                    elif carry_forward_between_iterations:
                        current_repo_path = selected_workspace.path
                    else:
                        current_repo_path = repo_path

        # Handoff summary from reviewers
        candidates_text = ""
        if final_selected_candidate:
            candidates_text = _candidate_summary_text(final_selected_candidate)
        _note("Generating handoff summaries from reviewers...")
        handoff_prompt = _review_candidates_prompt(
            task=task,
            candidates_text=candidates_text or "No candidates.",
            user_context=_format_user_context(user_qna),
            final_handoff=True,
        )
        reviewer_handoff: Dict[str, Dict[str, Any]] = {}

        def _handoff_one(reviewer: AgentSpec) -> Dict[str, Any]:
            return _run_with_agent_status(
                reviewer,
                phase="handoff",
                fn=lambda: _run_reviewer_decision(
                    reviewer,
                    codex_clients=codex_clients,
                    claude_clients=claude_clients,
                    prompt=handoff_prompt,
                    cwd=current_repo_path,
                    decision_schema=decision_schema,
                ),
            )

        with ThreadPoolExecutor(max_workers=len(reviewers)) as pool:
            futures = {pool.submit(_handoff_one, reviewer): reviewer for reviewer in reviewers}
            for future in as_completed(futures):
                reviewer = futures[future]
                reviewer_handoff[reviewer.id] = future.result()
        _note("Handoff complete.")

        state_manager.update_state("handoff", reviewer_handoff)
        if telegram_client:
            for reviewer_id, decision in reviewer_handoff.items():
                _send_telegram_message(
                    state_manager=state_manager,
                    telegram=telegram_client,
                    text=(
                        f"Reviewer {reviewer_id} summary:\n{decision.get('summary')}\n\n"
                        f"Next:\n{decision.get('next_prompt')}"
                    ),
                    label=f"handoff_summary:{reviewer_id}",
                )

        if not approved:
            state_manager.update_state("stage", "failed")
            state_manager.update_state("persisted", False)
        # Let the top-level runner clean up the root run workspace (not just the final candidate).
        # In session mode, we do per-session cleanup below since the top-level `finally` won't run.
        final_cleanup_workspace = None
        final_approved = approved
        final_persisted = persisted

        state_manager.update_state("run_status", "stopped")
        if session_mode:
            should_cleanup = (
                cleanup_policy == "always"
                or (cleanup_policy == "on_success" and approved and persisted)
                or force_cleanup_worktree
            )
            if should_cleanup:
                try:
                    _note("Cleaning up workspaces...")
                    run_dir = os.path.join(workspace_manager.base_dir, state_manager.run_id)
                    Workspace(
                        repo_path=original_repo_path,
                        path=original_repo_path,
                        strategy="in_place",
                        run_dir=run_dir,
                        baseline_path=None,
                        branch_name=None,
                    ).cleanup()
                    # Ensure we don't point to a deleted path between sessions.
                    current_repo_path = original_repo_path
                except Exception as e:
                    state_manager.add_to_history(f"Workspace cleanup failed: {e}")

            if delete_branch_after_merge and merge_branch_to_delete:
                _delete_local_branch(
                    original_repo_path,
                    merge_branch_to_delete,
                    note_fn=_note,
                )
        if not session_mode:
            break
        state_manager.update_state("run_status", "idle")
        state_manager.update_state("stage", "idle")
        _note("Run complete. Waiting for next task.")
        task = None

    return {
        "approved": final_approved,
        "persisted": final_persisted,
        "cleanup_workspace": final_cleanup_workspace,
        "force_cleanup_worktree": force_cleanup_worktree,
        "merge_branch_to_delete": merge_branch_to_delete,
        "delete_branch_on_merge": delete_branch_after_merge,
    }


def _find_resume_state(*, logs_root: str, repo_path: str) -> tuple[str, dict] | None:
    if not os.path.isdir(logs_root):
        return None
    repo_path = os.path.abspath(repo_path)
    candidates: list[tuple[float, str, dict]] = []
    for run_id in os.listdir(logs_root):
        run_dir = os.path.join(logs_root, run_id)
        state_path = os.path.join(run_dir, "state.json")
        if not os.path.isdir(run_dir) or not os.path.isfile(state_path):
            continue
        state = _read_json_file(state_path)
        if not isinstance(state, dict):
            continue
        if os.path.abspath(str(state.get("repo_path", ""))) != repo_path:
            continue
        if state.get("run_status") != "running":
            continue
        workspace_path = state.get("workspace_path")
        if workspace_path and not os.path.isdir(workspace_path):
            continue
        if state.get("workspace_strategy") == "copy" and workspace_path:
            baseline_path = os.path.join(os.path.dirname(workspace_path), "baseline")
            if not os.path.isdir(baseline_path):
                continue
        mtime = os.path.getmtime(state_path)
        candidates.append((mtime, run_id, state))
    if not candidates:
        return None
    _, run_id, state = max(candidates, key=lambda item: item[0])
    return run_id, state


def _validate_resume_run_id(run_id: str) -> str:
    run_id = str(run_id or "").strip()
    if not run_id:
        raise RuntimeError("Missing run id for resume.")
    if run_id in (".", ".."):
        raise RuntimeError("Invalid run id for resume.")
    if "\x00" in run_id:
        raise RuntimeError("Invalid run id for resume.")
    if os.path.isabs(run_id):
        raise RuntimeError("Invalid run id for resume (must be a directory name).")
    seps = [os.sep]
    if os.altsep:
        seps.append(os.altsep)
    if any(sep and sep in run_id for sep in seps):
        raise RuntimeError("Invalid run id for resume (must not contain path separators).")
    # Reject traversal-like values even if they don't contain separators (defense-in-depth).
    if ".." in run_id:
        raise RuntimeError("Invalid run id for resume.")
    return run_id


def _load_resume_state_by_id(*, logs_root: str, repo_path: str, run_id: str) -> tuple[str, dict]:
    run_id = _validate_resume_run_id(run_id)
    logs_root_abs = os.path.abspath(logs_root)
    run_dir = os.path.abspath(os.path.join(logs_root_abs, run_id))
    if os.path.commonpath([logs_root_abs, run_dir]) != logs_root_abs:
        raise RuntimeError("Invalid run id for resume.")
    state_path = os.path.join(run_dir, "state.json")
    if not os.path.isfile(state_path):
        raise RuntimeError(f"Cannot resume run {run_id}: state.json not found.")
    state = _read_json_file(state_path)
    if not isinstance(state, dict):
        raise RuntimeError(f"Cannot resume run {run_id}: invalid state.json.")
    if os.path.abspath(str(state.get("repo_path", ""))) != os.path.abspath(repo_path):
        raise RuntimeError("Cannot resume: run repo_path does not match current repo.")
    if state.get("run_completed") is True:
        raise RuntimeError(f"Cannot resume run {run_id}: run already completed.")
    return run_id, state


def _infer_resume_step(
    *,
    resume_stage: str | None,
    plan: dict | None,
    claude_structured: dict | None,
    implementation_result: str | None,
    test_results: dict | None,
    review: dict | None,
) -> str:
    if resume_stage in ("planning", "refine_plan"):
        return "planning"
    if resume_stage in ("plan_ready", "implementing"):
        return "implement"
    if resume_stage in ("implementation_ready", "testing"):
        return "tests"
    if resume_stage in ("tests_ready", "reviewing"):
        return "review"
    if resume_stage == "review_ready":
        if isinstance(review, dict) and review.get("status") == "REJECTED":
            return "next_iteration"
        if isinstance(review, dict) and review.get("status") == "APPROVED":
            return "persist"
        return "review"

    if isinstance(review, dict) and review.get("status") == "APPROVED":
        return "persist"
    if isinstance(review, dict) and review.get("status") == "REJECTED":
        return "next_iteration"
    if isinstance(test_results, dict):
        return "review"
    if isinstance(claude_structured, dict) and claude_structured.get("status") == "DONE" and implementation_result:
        return "tests"
    if isinstance(plan, dict) and plan.get("status") == "OK":
        return "implement"
    if isinstance(plan, dict) and plan.get("status") == "NEEDS_USER_INPUT":
        return "planning"
    return "planning"


def _resume_workspace(
    *,
    state: dict,
    workspace_manager: WorkspaceManager,
    repo_path: str,
    run_id: str,
) -> Workspace | None:
    strategy = state.get("workspace_strategy")
    workspace_path = state.get("workspace_path")
    run_dir = os.path.join(workspace_manager.base_dir, run_id)
    if strategy == "in_place":
        return Workspace(
            repo_path=repo_path,
            path=os.path.abspath(repo_path),
            strategy="in_place",
            run_dir=run_dir,
        )
    if strategy == "worktree" and isinstance(workspace_path, str) and os.path.isdir(workspace_path):
        return Workspace(
            repo_path=repo_path,
            path=workspace_path,
            strategy="worktree",
            run_dir=run_dir,
            baseline_path=None,
            branch_name=state.get("workspace_branch"),
        )
    if strategy == "copy" and isinstance(workspace_path, str):
        baseline_path = os.path.join(os.path.dirname(workspace_path), "baseline")
        if os.path.isdir(workspace_path) and os.path.isdir(baseline_path):
            return Workspace(
                repo_path=repo_path,
                path=workspace_path,
                strategy="copy",
                run_dir=run_dir,
                baseline_path=baseline_path,
            )
    return None


def _format_user_context(qna: list[dict]) -> str:
    lines: list[str] = []
    for item in qna:
        q = str(item.get("question", "")).strip()
        a = str(item.get("answer", "")).strip()
        if not q:
            continue
        lines.append(f"Q: {q}")
        lines.append(f"A: {a}")
        lines.append("")
    return "\n".join(lines).strip()


def _prompt_user_for_answers(
    questions: list[str],
    *,
    state_manager: StateManager,
    ui_active: bool,
    poll_interval_sec: float = 0.5,
    timeout_sec: float | None = None,
) -> list[dict]:
    questions_clean = [str(q).strip() for q in questions if str(q).strip()]
    if not questions_clean:
        return []

    existing = state_manager.get_state("awaiting_user_input")
    if isinstance(existing, dict) and existing.get("request_id"):
        request_id = str(existing.get("request_id"))
        existing_questions = existing.get("questions")
        if isinstance(existing_questions, list) and existing_questions:
            questions_clean = [str(q).strip() for q in existing_questions if str(q).strip()]
    else:
        request_id = str(uuid.uuid4())
        state_manager.update_state(
            "awaiting_user_input",
            {
                "request_id": request_id,
                "questions": questions_clean,
            },
        )
    state_manager.update_state("stage", "awaiting_user_input")

    request_path = os.path.join(state_manager.log_dir, f"user_input_request_{request_id}.json")
    response_path = os.path.join(state_manager.log_dir, f"user_input_response_{request_id}.json")
    if not os.path.exists(request_path):
        with open(request_path, "w") as f:
            json.dump({"request_id": request_id, "questions": questions_clean}, f, indent=2)

    def _finalize(answers: list[dict]) -> list[dict]:
        state_manager.update_state("awaiting_user_input", None)
        state_manager.update_state("stage", "planning")
        try:
            os.remove(request_path)
        except OSError:
            pass
        return answers

    # If UI is active, prefer answering via the web interface.
    if ui_active:
        print("")
        print("Codex requires user input. Please answer in the Luigi web UI.")
    elif sys.stdin.isatty():
        answers: list[dict] = []
        for q in questions_clean:
            print("")
            print("Codex question:")
            print(q)
            ans = input("> ").strip()
            answers.append({"question": q, "answer": ans})
        return _finalize(answers)

    # File-based answer flow (for web UI or non-TTY execution).
    start = time.time()
    while True:
        if os.path.exists(response_path):
            try:
                with open(response_path, "r") as f:
                    payload = json.load(f)
            except json.JSONDecodeError:
                payload = None
            if not isinstance(payload, dict):
                time.sleep(poll_interval_sec)
                continue
            try:
                os.remove(response_path)
            except OSError:
                pass

            answers = payload.get("answers", [])
            if not isinstance(answers, list):
                answers = []
            return _finalize(answers)

        if timeout_sec is not None and (time.time() - start) > timeout_sec:
            raise RuntimeError("Timed out waiting for user input.")

        time.sleep(poll_interval_sec)


def _prompt_user_for_initial_task(
    *,
    state_manager: StateManager,
    ui_active: bool,
    telegram: Optional[TelegramClient] = None,
    poll_interval_sec: float = 0.5,
    timeout_sec: float | None = None,
) -> str:
    existing = state_manager.get_state("awaiting_initial_task")
    if isinstance(existing, dict) and existing.get("request_id"):
        request_id = str(existing.get("request_id"))
    else:
        request_id = str(uuid.uuid4())
        state_manager.update_state("awaiting_initial_task", {"request_id": request_id})
    state_manager.update_state("stage", "awaiting_initial_task")
    state_manager.add_to_history(f"Awaiting initial task request_id={request_id}.")

    request_path = os.path.join(state_manager.log_dir, f"initial_task_request_{request_id}.json")
    response_path = os.path.join(state_manager.log_dir, f"initial_task_response_{request_id}.json")
    if not os.path.exists(request_path):
        with open(request_path, "w") as f:
            json.dump({"request_id": request_id}, f, indent=2)

    def _finalize(task_text: str) -> str:
        state_manager.update_state("awaiting_initial_task", None)
        state_manager.update_state("stage", "planning")
        try:
            os.remove(request_path)
        except OSError:
            pass
        return task_text

    if ui_active:
        print("")
        print("Please open the Luigi web UI and enter the initial task to start.")
    elif sys.stdin.isatty():
        task_text = input("Task> ").strip()
        if not task_text:
            raise RuntimeError("Empty task provided.")
        return _finalize(task_text)

    if telegram:
        lines = [
            "New task required. Reply with:",
            f"request_id: {request_id}",
            "task: <your task>",
        ]
        _send_telegram_message(
            state_manager=state_manager,
            telegram=telegram,
            text="\n".join(lines),
            label=f"initial_task_request:{request_id}",
        )

    start = time.time()
    offset = state_manager.get_state("telegram_update_offset")
    if not isinstance(offset, int):
        offset = None
    while True:
        if os.path.exists(response_path):
            try:
                with open(response_path, "r") as f:
                    payload = json.load(f)
            except json.JSONDecodeError:
                payload = None
            if not isinstance(payload, dict):
                time.sleep(poll_interval_sec)
                continue
            try:
                os.remove(response_path)
            except OSError:
                pass

            task_text = str(payload.get("task", "")).strip()
            if not task_text:
                raise RuntimeError("Empty task provided via UI.")
            state_manager.add_to_history("Initial task received via UI.")
            return _finalize(task_text)

        if telegram:
            updates = telegram.poll_updates(offset)
            for item in updates.get("result", []):
                update_id = item.get("update_id")
                if isinstance(update_id, int):
                    next_offset = update_id + 1
                    if offset is None or next_offset > offset:
                        offset = next_offset
                        state_manager.update_state("telegram_update_offset", offset)
            for message in telegram.filter_messages(updates):
                text = str(message.get("text", "")).strip()
                if not text:
                    continue
                parsed = _parse_task_message(text)
                if parsed.get("request_id") != request_id:
                    continue
                task_text = str(parsed.get("task", "")).strip()
                if not task_text:
                    continue
                state_manager.add_to_history("Initial task received via Telegram.")
                return _finalize(task_text)

        if timeout_sec is not None and (time.time() - start) > timeout_sec:
            raise RuntimeError("Timed out waiting for initial task.")

        time.sleep(poll_interval_sec)


CLAUDE_STRUCTURED_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status"],
    "properties": {
        # Back-compat: accept NEEDS_CODEX, but prefer NEEDS_REVIEWER in multi-agent runs.
        "status": {"type": "string", "enum": ["DONE", "NEEDS_REVIEWER", "NEEDS_CODEX", "FAILED"]},
        "questions": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
}

CLAUDE_APPEND_SYSTEM_PROMPT = (
    "You are running under Luigi orchestration in non-interactive mode.\n"
    "If you need clarification, DO NOT ask the user.\n"
    "Instead, set structured_output.status=\"NEEDS_REVIEWER\" and populate structured_output.questions.\n"
    "Back-compat: structured_output.status=\"NEEDS_CODEX\" is also accepted.\n"
    "When you have completed the requested work, set structured_output.status=\"DONE\" and provide a short summary.\n"
    "If you cannot proceed, set structured_output.status=\"FAILED\" and explain in the summary.\n"
)


def _get_claude_structured(output: dict) -> dict:
    structured = output.get("structured_output")
    if isinstance(structured, dict):
        return structured
    # Back-compat with older/mocked outputs: treat as DONE.
    return {"status": "DONE", "summary": output.get("result", "")}


def _run_git(cmd: List[str], *, cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def _git_error(cmd: List[str], result: subprocess.CompletedProcess) -> str:
    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    details = []
    if stdout:
        details.append(f"stdout: {stdout}")
    if stderr:
        details.append(f"stderr: {stderr}")
    detail_text = f" ({' | '.join(details)})" if details else ""
    return f"Command failed: {' '.join(cmd)}{detail_text}"


def _git_current_branch(repo_path: str) -> str:
    result = _run_git(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_path)
    if result.returncode != 0:
        raise RuntimeError(_git_error(["git", "rev-parse", "--abbrev-ref", "HEAD"], result))
    branch = (result.stdout or "").strip()
    if not branch:
        raise RuntimeError("Unable to determine current git branch.")
    return branch


def _git_status_porcelain(repo_path: str) -> str:
    result = _run_git(["git", "status", "--porcelain"], cwd=repo_path)
    if result.returncode != 0:
        raise RuntimeError(_git_error(["git", "status", "--porcelain"], result))
    return result.stdout or ""


def _git_commit_all(repo_path: str, message: str) -> Optional[str]:
    status = _git_status_porcelain(repo_path)
    if not status.strip():
        return None
    add_res = _run_git(["git", "add", "-A"], cwd=repo_path)
    if add_res.returncode != 0:
        raise RuntimeError(_git_error(["git", "add", "-A"], add_res))
    commit_res = _run_git(["git", "commit", "-m", message], cwd=repo_path)
    if commit_res.returncode != 0:
        raise RuntimeError(_git_error(["git", "commit", "-m", message], commit_res))
    head_res = _run_git(["git", "rev-parse", "HEAD"], cwd=repo_path)
    if head_res.returncode != 0:
        raise RuntimeError(_git_error(["git", "rev-parse", "HEAD"], head_res))
    return (head_res.stdout or "").strip() or None


def _git_checkout_branch(repo_path: str, branch: str) -> None:
    result = _run_git(["git", "checkout", branch], cwd=repo_path)
    if result.returncode != 0:
        raise RuntimeError(_git_error(["git", "checkout", branch], result))


def _git_unmerged_files(repo_path: str) -> List[str]:
    result = _run_git(["git", "diff", "--name-only", "--diff-filter=U"], cwd=repo_path)
    if result.returncode != 0:
        raise RuntimeError(_git_error(["git", "diff", "--name-only", "--diff-filter=U"], result))
    return [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]


def _git_is_merge_in_progress(repo_path: str) -> bool:
    result = _run_git(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd=repo_path)
    return result.returncode == 0


def _git_head_sha(repo_path: str) -> Optional[str]:
    result = _run_git(["git", "rev-parse", "HEAD"], cwd=repo_path)
    if result.returncode != 0:
        raise RuntimeError(_git_error(["git", "rev-parse", "HEAD"], result))
    return (result.stdout or "").strip() or None


def _git_is_ancestor(repo_path: str, ancestor: str, descendant: str) -> bool:
    result = _run_git(["git", "merge-base", "--is-ancestor", ancestor, descendant], cwd=repo_path)
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise RuntimeError(_git_error(["git", "merge-base", "--is-ancestor", ancestor, descendant], result))


def _worktree_path_for_branch(repo_path: str, branch_name: str) -> Optional[str]:
    result = _run_git(["git", "worktree", "list", "--porcelain"], cwd=repo_path)
    if result.returncode != 0:
        raise RuntimeError(_git_error(["git", "worktree", "list", "--porcelain"], result))
    path: Optional[str] = None
    branch_ref = f"refs/heads/{branch_name}"
    for line in (result.stdout or "").splitlines():
        if line.startswith("worktree "):
            path = line.split(" ", 1)[1].strip()
            continue
        if line.startswith("branch ") and path:
            ref = line.split(" ", 1)[1].strip()
            if ref == branch_ref:
                return path
            path = None
    return None


def _delete_local_branch(
    repo_path: str, branch_name: str, note_fn: Optional[Callable[[str], None]] = None
) -> bool:
    ref_check = _run_git(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"], cwd=repo_path
    )
    if ref_check.returncode != 0:
        return False

    in_use_path = _worktree_path_for_branch(repo_path, branch_name)
    if in_use_path:
        if note_fn:
            note_fn(f"Skipping branch delete; still checked out at {in_use_path}")
        return False

    result = _run_git(["git", "branch", "-d", branch_name], cwd=repo_path)
    if result.returncode != 0:
        if note_fn:
            note_fn(_git_error(["git", "branch", "-d", branch_name], result))
        return False
    if note_fn:
        note_fn(f"Deleted local branch: {branch_name}")
    return True


def _truncate_lines(text: str, max_lines: int) -> str:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    return "\n".join(lines[:max_lines] + ["... (truncated)"])


def _format_plan_for_merge(plan: Optional[dict]) -> str:
    if not isinstance(plan, dict):
        return "No plan available."
    summary: Dict[str, Any] = {}
    for key in ("notes", "tasks", "test_commands"):
        value = plan.get(key)
        if value:
            summary[key] = value
    claude_prompt = plan.get("claude_prompt")
    if isinstance(claude_prompt, str) and claude_prompt.strip():
        summary["claude_prompt_excerpt"] = _truncate_lines(claude_prompt, 40)
    return json.dumps(summary, indent=2) if summary else "Plan contained no additional details."


def _format_review_for_merge(decisions: Optional[dict]) -> str:
    if not isinstance(decisions, dict) or not decisions:
        return "No reviewer decisions available."
    lines = []
    for reviewer_id, decision in decisions.items():
        if not isinstance(decision, dict):
            continue
        status = decision.get("status")
        winner = decision.get("winner_candidate_id")
        summary = decision.get("summary")
        feedback = decision.get("feedback")
        notes = decision.get("notes")
        lines.append(
            f"- {reviewer_id}: status={status} winner={winner}\n"
            f"  summary={summary}\n"
            f"  feedback={feedback}\n"
            f"  notes={notes}"
        )
    return "\n".join(lines) if lines else "No reviewer decisions available."


def _format_candidate_for_merge(candidate: Optional[dict]) -> str:
    if not isinstance(candidate, dict):
        return "No candidate context available."
    parts = [
        f"id: {candidate.get('id')}",
        f"reviewer_id: {candidate.get('reviewer_id')}",
        f"executor_id: {candidate.get('executor_id')}",
        f"workspace_path: {candidate.get('workspace_path')}",
        f"workspace_strategy: {candidate.get('workspace_strategy')}",
        f"status: {candidate.get('status')}",
        f"test_summary: {candidate.get('test_summary')}",
    ]
    diff_preview = candidate.get("diff_preview") or ""
    if isinstance(diff_preview, str) and diff_preview.strip():
        parts.append("diff_preview:\n" + _truncate_lines(diff_preview, 120))
    return "\n".join(parts)


def _build_merge_conflict_prompt(
    *,
    task: Optional[str],
    branch_name: str,
    target_branch: str,
    merge_message: str,
    merge_output: str,
    conflict_files: List[str],
    plan_context: str,
    review_context: str,
    candidate_context: str,
    status_porcelain: str,
) -> str:
    conflict_list = "\n".join(f"- {path}" for path in conflict_files) if conflict_files else "(none)"
    return "\n".join(
        [
            "You are resolving git merge conflicts for Luigi's orchestrator.",
            f"Task: {task or '(no task provided)'}",
            f"Source branch: {branch_name}",
            f"Target branch: {target_branch}",
            "",
            "Context from the approved work:",
            "Plan context:",
            plan_context,
            "",
            "Reviewer context:",
            review_context,
            "",
            "Candidate context:",
            candidate_context,
            "",
            "Merge output:",
            _truncate_lines(merge_output or "", 40),
            "",
            "Conflicted files:",
            conflict_list,
            "",
            "git status --porcelain:",
            _truncate_lines(status_porcelain or "", 40),
            "",
            "Instructions:",
            "- Resolve conflicts in the repo using the plan + review context.",
            "- Prefer the approved worktree branch changes unless the reviews say otherwise.",
            "- After resolving, stage the files with git add.",
            f"- Complete the merge commit using: git commit -m \"{merge_message}\"",
            "- Ensure there are no unmerged paths (git diff --name-only --diff-filter=U should be empty).",
            "- Do not run tests unless needed for conflict resolution.",
        ]
    )


def _pick_merge_claude_client(
    claude_clients: Dict[str, ClaudeCodeClient], preferred_id: Optional[str] = None
) -> Optional[ClaudeCodeClient]:
    if preferred_id and preferred_id in claude_clients:
        return claude_clients[preferred_id]
    if claude_clients:
        return next(iter(claude_clients.values()))
    return None


def _auto_merge_worktree_branch(
    *,
    repo_path: str,
    branch_name: Optional[str],
    target_branch: str,
    merge_style: str,
    dirty_main_policy: str,
    dirty_main_commit_message_template: str,
    merge_commit_message: str,
    claude_client: Optional[ClaudeCodeClient],
    task: Optional[str],
    run_id: str,
    plan: Optional[dict],
    reviewer_decisions: Optional[dict],
    candidate: Optional[dict],
    note_fn: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    def _note(msg: str) -> None:
        if note_fn:
            note_fn(msg)

    result: Dict[str, Any] = {"merged": False, "conflicts_resolved": False}
    if not branch_name:
        result["error"] = "Missing worktree branch name; cannot auto-merge."
        return result

    ref_check = _run_git(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{target_branch}"], cwd=repo_path
    )
    if ref_check.returncode != 0:
        result["error"] = f"Target branch not found: {target_branch}"
        return result

    if merge_style != "merge_commit":
        result["error"] = f"Unsupported merge_style: {merge_style}"
        return result

    try:
        current_branch = _git_current_branch(repo_path)
        if current_branch != target_branch:
            _note(f"Checking out target branch: {target_branch}")
            try:
                _git_checkout_branch(repo_path, target_branch)
            except RuntimeError as exc:
                if dirty_main_policy != "commit":
                    raise
                _note("Target checkout failed; committing local changes before retry.")
                commit_sha = _git_commit_all(
                    repo_path,
                    dirty_main_commit_message_template.format(
                        task=task or "",
                        run_id=run_id,
                        branch=current_branch,
                        target=target_branch,
                    ),
                )
                result["dirty_main_commit_sha"] = commit_sha
                _git_checkout_branch(repo_path, target_branch)
        status = _git_status_porcelain(repo_path)
        if status.strip():
            if dirty_main_policy == "commit":
                _note("Uncommitted changes detected on target branch; auto-committing.")
                commit_sha = _git_commit_all(
                    repo_path,
                    dirty_main_commit_message_template.format(
                        task=task or "",
                        run_id=run_id,
                        branch=target_branch,
                        target=target_branch,
                    ),
                )
                result["dirty_main_commit_sha"] = commit_sha
            elif dirty_main_policy == "abort":
                raise RuntimeError("Target branch has uncommitted changes; aborting merge.")
            else:
                raise RuntimeError(f"Unsupported dirty_main_policy: {dirty_main_policy}")

        merge_cmd = ["git", "merge", "--no-ff", "-m", merge_commit_message, branch_name]
        _note(f"Merging {branch_name} into {target_branch}...")
        merge_res = _run_git(merge_cmd, cwd=repo_path)
        if merge_res.returncode == 0:
            if not _git_is_ancestor(repo_path, branch_name, target_branch):
                raise RuntimeError("Merge completed but branch is not merged into target.")
            result["merged"] = True
            result["merge_commit_sha"] = _git_head_sha(repo_path)
            return result

        conflict_files = _git_unmerged_files(repo_path)
        result["conflict_files"] = conflict_files
        if not conflict_files:
            raise RuntimeError(_git_error(merge_cmd, merge_res))

        if not claude_client:
            raise RuntimeError("Merge conflicts detected, but no Claude Code client is available.")

        status_porcelain = _git_status_porcelain(repo_path)
        prompt = _build_merge_conflict_prompt(
            task=task,
            branch_name=branch_name,
            target_branch=target_branch,
            merge_message=merge_commit_message,
            merge_output="\n".join([merge_res.stdout or "", merge_res.stderr or ""]).strip(),
            conflict_files=conflict_files,
            plan_context=_format_plan_for_merge(plan),
            review_context=_format_review_for_merge(reviewer_decisions),
            candidate_context=_format_candidate_for_merge(candidate),
            status_porcelain=status_porcelain,
        )
        _note("Attempting conflict resolution with Claude Code...")
        output = claude_client.implement(
            prompt,
            cwd=repo_path,
            json_schema=CLAUDE_STRUCTURED_SCHEMA,
            append_system_prompt=CLAUDE_APPEND_SYSTEM_PROMPT,
        )
        structured = _get_claude_structured(output or {})
        result["claude_merge_status"] = structured.get("status")
        result["claude_merge_summary"] = structured.get("summary")
        if structured.get("status") != "DONE":
            raise RuntimeError(f"Claude conflict resolution did not complete: {structured}")

        remaining_conflicts = _git_unmerged_files(repo_path)
        if remaining_conflicts:
            raise RuntimeError(f"Conflicts remain after Claude resolution: {remaining_conflicts}")

        if _git_is_merge_in_progress(repo_path):
            commit_res = _run_git(["git", "commit", "-m", merge_commit_message], cwd=repo_path)
            if commit_res.returncode != 0:
                raise RuntimeError(_git_error(["git", "commit", "-m", merge_commit_message], commit_res))

        if not _git_is_ancestor(repo_path, branch_name, target_branch):
            raise RuntimeError("Branch does not appear merged after conflict resolution.")

        result["merged"] = True
        result["conflicts_resolved"] = True
        result["merge_commit_sha"] = _git_head_sha(repo_path)
        return result
    except Exception as exc:
        result["error"] = str(exc)
        return result


def resolve_config_path(config_arg: str | None, *, repo_path: str) -> str:
    """Resolve config file path.

    Precedence:
    1) Explicit `--config` argument (path is used as-is)
    2) Repo-local config files (JSON/YAML)
    3) Built-in default `config.json` shipped with the tool
    """
    if config_arg:
        return config_arg

    candidates = [
        # Preferred (new name)
        os.path.join(repo_path, ".luigi", "config.json"),
        os.path.join(repo_path, ".luigi", "config.yaml"),
        os.path.join(repo_path, ".luigi", "config.yml"),
        os.path.join(repo_path, "luigi.config.json"),
        os.path.join(repo_path, "luigi.config.yaml"),
        os.path.join(repo_path, "luigi.config.yml"),
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate

    return os.path.join(os.path.dirname(__file__), "config.yaml")


def main():
    """Main function to run the orchestration loop."""
    parser = argparse.ArgumentParser(description="Luigi: Codex + Claude Code automated coding orchestrator")
    parser.add_argument(
        "task_or_repo",
        nargs="?",
        default=None,
        help='Task prompt, or a repo path (e.g. "." to start UI-first mode).',
    )
    parser.add_argument("--repo", type=str, default=None, help="Path to the target repository/workspace.")
    parser.add_argument(
        "--resume-run-id",
        type=str,
        default=None,
        help="Resume a specific Luigi run id from logs.",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to config file (JSON or YAML). If omitted, uses repo-local config or built-in defaults.",
    )
    args = parser.parse_args()

    # CLI behavior:
    # - `luigi "do X"` → run in current directory
    # - `luigi --repo /path "do X"` → run in specified directory
    # - `luigi .` or `luigi /path/to/repo` → UI-first mode, collect initial task in web UI
    task: str | None = None
    if args.repo:
        repo_path = os.path.abspath(args.repo)
        task = args.task_or_repo
    else:
        candidate = args.task_or_repo
        if candidate is None:
            repo_path = os.getcwd()
            task = None
        elif os.path.isdir(candidate):
            repo_path = os.path.abspath(candidate)
            task = None
        else:
            repo_path = os.getcwd()
            task = candidate
    if args.resume_run_id and task is not None:
        raise SystemExit("Cannot combine --resume-run-id with an explicit task prompt.")

    config_path = resolve_config_path(args.config, repo_path=repo_path)
    config = load_config(config_path)

    logs_root = _normalize_path(
        config.get("orchestrator", {}).get("logs_dir", "~/.luigi/logs"),
        repo_path=repo_path,
    )
    resume_on_start = bool(config.get("orchestrator", {}).get("resume_on_start", True))
    resume_info = None
    if args.resume_run_id:
        resume_info = _load_resume_state_by_id(
            logs_root=logs_root,
            repo_path=repo_path,
            run_id=args.resume_run_id,
        )
    elif resume_on_start and task is None:
        resume_info = _find_resume_state(logs_root=logs_root, repo_path=repo_path)
    if resume_info:
        resume_run_id, _ = resume_info
        state_manager = StateManager(logs_root=logs_root, run_id=resume_run_id, load_existing=True)
    else:
        state_manager = StateManager(logs_root=logs_root)

    codex_cfg = dict(config["codex"])
    codex_cfg["log_dir"] = state_manager.log_dir
    claude_cfg = dict(config["claude_code"])
    claude_cfg["log_dir"] = state_manager.log_dir
    codex_client = CodexClient(codex_cfg)
    claude_code_client = ClaudeCodeClient(claude_cfg)

    telegram_cfg = config.get("telegram", {}) if isinstance(config, dict) else {}
    telegram_client = None
    if telegram_cfg.get("enabled"):
        allowed_user_ids = [
            int(x) for x in telegram_cfg.get("allowed_user_ids", []) if str(x).isdigit()
        ]
        if not allowed_user_ids:
            print(
                "Warning: Telegram is enabled with empty allowed_user_ids. "
                "Any user in the configured chat can respond."
            )
        telegram_client = TelegramClient(
            bot_token=str(telegram_cfg.get("bot_token") or ""),
            chat_id=str(telegram_cfg.get("chat_id") or ""),
            allowed_user_ids=allowed_user_ids,
            poll_interval_sec=float(telegram_cfg.get("poll_interval_sec", 2.0)),
        )

    agents = normalize_agents(config)
    reviewers = agents["reviewers"]
    executors = agents["executors"]
    assignment = assignment_config(config)
    resuming = resume_info is not None
    session_mode = bool(config.get("orchestrator", {}).get("session_mode", False))
    multi_agent_enabled = len(reviewers) > 1 or len(executors) > 1 or bool(
        config.get("orchestrator", {}).get("multi_agent")
    )
    stored_mode = state_manager.state.get("orchestrator_mode") if resuming else None
    if stored_mode in ("single", "multi"):
        # On resume, honor the mode used when the run started to avoid resuming into the wrong loop.
        multi_agent_enabled = stored_mode == "multi"
    elif session_mode and not resuming:
        multi_agent_enabled = True
    if resuming:
        state_manager.add_to_history("Resuming previous run.")
    # Persist core run metadata early for monitoring.
    state_manager.update_state("run_id", state_manager.run_id)
    state_manager.update_state("repo_path", repo_path)
    state_manager.update_state("config_path", os.path.abspath(config_path))
    state_manager.update_state("run_status", "running")
    state_manager.update_state(
        "agents",
        {
            "reviewers": [{"id": r.id, "kind": r.kind} for r in reviewers],
            "executors": [{"id": e.id, "kind": e.kind} for e in executors],
        },
    )
    state_manager.update_state("codex_status", "Stopped")
    state_manager.update_state("claude_status", "Stopped")
    state_manager.update_state("codex_phase", "idle")
    state_manager.update_state("claude_phase", "idle")
    state_manager.update_state("codex_log_path", os.path.join(state_manager.log_dir, "codex.log"))
    state_manager.update_state("claude_log_path", os.path.join(state_manager.log_dir, "claude.log"))
    state_manager.update_state("orchestrator_mode", "multi" if multi_agent_enabled else "single")

    invocation_dir = os.getcwd()
    project_id = compute_project_id(invocation_dir)
    state_manager.update_state("project_id", project_id)

    ui_cfg = config.get("orchestrator", {}).get("ui", {})
    ui_enabled = bool(ui_cfg.get("enabled", True)) or task is None
    ui_host = str(ui_cfg.get("host", "127.0.0.1"))
    ui_base_port = int(ui_cfg.get("base_port", 8501))
    ui_port_range = int(ui_cfg.get("port_range", 500))
    ui_open_browser = bool(ui_cfg.get("open_browser", False)) or task is None
    ui_keep_alive_after_run = bool(ui_cfg.get("keep_alive_after_run", False))
    user_input_poll_interval_sec = float(ui_cfg.get("poll_interval_sec", 0.5))
    user_input_timeout_sec = ui_cfg.get("user_input_timeout_sec")
    if user_input_timeout_sec is not None:
        user_input_timeout_sec = float(user_input_timeout_sec)

    ui = start_streamlit_ui(
        log_dir=state_manager.log_dir,
        run_id=state_manager.run_id,
        repo_path=repo_path,
        invocation_dir=invocation_dir,
        enabled=ui_enabled,
        host=ui_host,
        base_port=ui_base_port,
        port_range=ui_port_range,
        open_browser=ui_open_browser,
    )
    if ui:
        state_manager.update_state(
            "ui",
            {
                "enabled": True,
                "url": ui.url,
                "port": ui.port,
                "host": ui.host,
                "log_path": ui.log_path,
                "project_id": ui.project_id,
            },
        )
    else:
        state_manager.update_state(
            "ui",
            {
                "enabled": False,
                "project_id": project_id,
            },
        )

    workspace_base_dir = _normalize_path(
        config.get("orchestrator", {}).get("working_dir", "~/.luigi/workspaces"),
        repo_path=repo_path,
    )
    workspace_strategy = config.get("orchestrator", {}).get("workspace_strategy", "in_place")
    use_git_worktree = config.get("orchestrator", {}).get("use_git_worktree", True)
    workspace_manager = WorkspaceManager(workspace_base_dir)

    resume_state = state_manager.state if resuming else {}
    if resuming and not task:
        task = resume_state.get("task")

    iteration = int(resume_state.get("iteration") or 0)
    approved = bool(resume_state.get("approved")) if resuming else False
    claude_session_id = resume_state.get("claude_session_id") if resuming else None
    plan = resume_state.get("plan") if resuming else None
    review = resume_state.get("review") if resuming else None
    implementation_result = resume_state.get("implementation_result") if resuming else ""
    test_results = resume_state.get("test_results") if resuming else None
    persisted = bool(resume_state.get("persisted")) if resuming else False
    user_qna = resume_state.get("user_qna") if resuming else state_manager.get_state("user_qna")
    if user_qna is None:
        user_qna = []
    if not isinstance(user_qna, list):
        user_qna = []

    cleanup_policy = config.get("orchestrator", {}).get("cleanup", "on_success")  # always | on_success | never
    apply_changes_on_success = config.get("orchestrator", {}).get("apply_changes_on_success", True)
    commit_on_approval = config.get("orchestrator", {}).get("commit_on_approval", True)
    commit_message_template = config.get("orchestrator", {}).get("commit_message", "Task complete: {task}")
    auto_merge_on_approval = bool(config.get("orchestrator", {}).get("auto_merge_on_approval", False))
    merge_target_branch = config.get("orchestrator", {}).get("merge_target_branch", "main")
    merge_style = config.get("orchestrator", {}).get("merge_style", "merge_commit")
    branch_prefix = config.get("orchestrator", {}).get("branch_prefix", "luigi")
    branch_name_length = _optional_positive_int(
        config.get("orchestrator", {}).get("branch_name_length", 8),
        default=8,
    ) or 8
    dirty_main_policy = config.get("orchestrator", {}).get("dirty_main_policy", "commit")
    dirty_main_commit_message_template = config.get(
        "orchestrator",
        {},
    ).get(
        "dirty_main_commit_message",
        "Auto-commit local changes before Luigi merge (run {run_id})",
    )
    merge_commit_message_template = config.get(
        "orchestrator",
        {},
    ).get(
        "merge_commit_message",
        "Merge {branch} into {target} (run {run_id})",
    )
    delete_branch_on_merge = bool(config.get("orchestrator", {}).get("delete_branch_on_merge", True))
    delete_worktree_on_merge = bool(config.get("orchestrator", {}).get("delete_worktree_on_merge", True))
    max_claude_question_rounds = _optional_positive_int(
        config.get("orchestrator", {}).get("max_claude_question_rounds", 5),
        default=5,
    )

    resume_stage = resume_state.get("stage") if resuming else None
    resume_step = None
    if resuming:
        resume_step = _infer_resume_step(
            resume_stage=resume_stage if isinstance(resume_stage, str) else None,
            plan=plan if isinstance(plan, dict) else None,
            claude_structured=resume_state.get("claude_structured_output")
            if isinstance(resume_state.get("claude_structured_output"), dict)
            else None,
            implementation_result=implementation_result,
            test_results=test_results if isinstance(test_results, dict) else None,
            review=review if isinstance(review, dict) else None,
        )

    if resuming and resume_step in ("planning", "implement", "tests", "review"):
        iteration = max(iteration - 1, 0)
    if resuming and resume_step == "persist":
        approved = True
        state_manager.update_state("approved", True)
    if resuming and resume_step:
        state_manager.update_state("resume_step", resume_step)

    print(f"Run ID: {state_manager.run_id}")
    print(f"Repo:   {repo_path}")
    print(f"Config: {os.path.abspath(config_path)}")
    print(f"Logs:   {state_manager.log_dir}")
    if ui:
        print(f"UI:     {ui.url} (project: {ui.project_id})")
    elif ui_enabled:
        print("UI:     (disabled) Install Python deps with: python3 -m pip install -r requirements.txt")

    workspace = None
    if resuming:
        workspace = _resume_workspace(
            state=resume_state,
            workspace_manager=workspace_manager,
            repo_path=repo_path,
            run_id=state_manager.run_id,
        )
        if workspace:
            state_manager.add_to_history("Resumed existing workspace.")
    if workspace is None:
        workspace = workspace_manager.create(
            repo_path=repo_path,
            run_id=state_manager.run_id,
            strategy=workspace_strategy,
            use_git_worktree=use_git_worktree,
            branch_prefix=branch_prefix,
            branch_name_length=branch_name_length,
        )
    state_manager.update_state("workspace_path", workspace.path)
    state_manager.update_state("workspace_strategy", workspace.strategy)
    if workspace.branch_name:
        state_manager.update_state("workspace_branch", workspace.branch_name)
    print(f"Workspace: {workspace.path} ({workspace.strategy})")

    cleanup_workspace = workspace
    run_completed = False
    force_cleanup_worktree = False
    merge_branch_to_delete = None
    delete_branch_on_merge = False

    def _with_codex_status(phase: str, fn):
        state_manager.update_state("codex_status", "Running")
        state_manager.update_state("codex_phase", phase)
        try:
            return fn()
        finally:
            state_manager.update_state("codex_status", "Stopped")
            state_manager.update_state("codex_phase", "idle")

    def _with_claude_status(phase: str, fn):
        state_manager.update_state("claude_status", "Running")
        state_manager.update_state("claude_phase", phase)
        try:
            return fn()
        finally:
            state_manager.update_state("claude_status", "Stopped")
            state_manager.update_state("claude_phase", "idle")

    try:
        if multi_agent_enabled:
            multi_result = run_multi_agent_session(
                task=task,
                config=config,
                state_manager=state_manager,
                workspace_manager=workspace_manager,
                reviewers=reviewers,
                executors=executors,
                assignment=assignment,
                repo_path=repo_path,
                ui=ui,
                telegram_client=telegram_client,
                user_input_poll_interval_sec=user_input_poll_interval_sec,
                user_input_timeout_sec=user_input_timeout_sec,
                resuming=resuming,
            )
            approved = bool(multi_result.get("approved"))
            persisted = bool(multi_result.get("persisted"))
            cleanup_workspace = multi_result.get("cleanup_workspace") or workspace
            force_cleanup_worktree = bool(multi_result.get("force_cleanup_worktree"))
            merge_branch_to_delete = multi_result.get("merge_branch_to_delete")
            delete_branch_on_merge = bool(multi_result.get("delete_branch_on_merge"))
            run_completed = True
        else:
            if not task:
                task = _prompt_user_for_initial_task(
                    state_manager=state_manager,
                    ui_active=ui is not None and ui.is_running(),
                    telegram=telegram_client,
                    poll_interval_sec=user_input_poll_interval_sec,
                    timeout_sec=user_input_timeout_sec,
                )
                state_manager.update_state("task", task)

            resume_used = False
            max_iterations = _optional_positive_int(
                config.get("orchestrator", {}).get("max_iterations", 5),
                default=5,
            )
            while not approved:
                next_iteration = iteration + 1
                if max_iterations is not None and next_iteration > max_iterations:
                    ui_active = ui is not None and ui.is_running()
                    if not ui_active and not telegram_client:
                        break

                    missing_summary = str(state_manager.get_state("feedback") or "").strip()
                    if not missing_summary:
                        review_obj = state_manager.get_state("review")
                        if isinstance(review_obj, dict):
                            missing_summary = str(review_obj.get("feedback") or "").strip()

                    if telegram_client and missing_summary:
                        _send_telegram_message(
                            state_manager=state_manager,
                            telegram=telegram_client,
                            text=(
                                f"Max iterations reached (iteration {iteration} / {max_iterations}).\n\n"
                                "Summary of remaining work (from reviewer feedback):\n"
                                f"{missing_summary}"
                            ),
                            label="max_iterations_summary",
                        )

                    preview = _preview_one_line(missing_summary, max_len=160) or "(no summary available)"
                    extend_by = 5
                    options = [
                        {
                            "label": f"Stop now and accept partial result (missing: {preview})",
                            "action": "accept_partial",
                            "missing_summary": missing_summary,
                            "iteration": iteration,
                            "max_iterations": max_iterations,
                        },
                        {
                            "label": f"Continue for {extend_by} more iterations",
                            "action": "extend",
                            "extend_by": extend_by,
                            "iteration": iteration,
                            "max_iterations": max_iterations,
                        },
                    ]
                    admin_choice = _await_admin_decision(
                        state_manager=state_manager,
                        options=options,
                        ui_active=ui_active,
                        telegram=telegram_client,
                        poll_interval_sec=user_input_poll_interval_sec,
                        timeout_sec=user_input_timeout_sec,
                    )
                    choice_idx = int(admin_choice.get("choice") or 1) - 1
                    choice_idx = max(0, min(choice_idx, len(options) - 1))
                    selection = options[choice_idx]
                    if selection.get("action") == "extend":
                        max_iterations = int(max_iterations) + int(selection.get("extend_by") or extend_by)
                        print(f"Admin extended max_iterations to {max_iterations}.")
                        state_manager.add_to_history(f"Admin extended max_iterations to {max_iterations}.")
                        continue

                    state_manager.add_to_history(
                        "Admin accepted partial result after reaching max iterations."
                    )
                    state_manager.update_state("max_iterations_missing_summary", missing_summary)
                    state_manager.update_state("approved_by_admin", True)
                    approved = True
                    state_manager.update_state("approved", True)
                    break

                iteration = next_iteration
                state_manager.update_state("iteration", iteration)
                print(f"--- Starting Iteration {iteration} ---")
                state_manager.add_to_history(f"Iteration {iteration}")

                # 1. Planning (or re-planning based on feedback)
                skip_plan = False
                skip_implement = False
                skip_tests = False
                skip_review = False
                if resuming and not resume_used and resume_step in ("implement", "tests", "review"):
                    skip_plan = True
                if resuming and not resume_used and resume_step in ("tests", "review"):
                    skip_implement = True
                if resuming and not resume_used and resume_step == "review":
                    skip_tests = True

                if iteration == 1:
                    print("Codex is creating the initial plan...")
                    while True:
                        if skip_plan and isinstance(plan, dict) and plan.get("status") != "NEEDS_USER_INPUT":
                            print("Resuming from existing plan.")
                            state_manager.update_state("plan", plan)
                            state_manager.update_state("stage", "plan_ready")
                            break
                        state_manager.update_state("stage", "planning")
                        plan = _with_codex_status(
                            "plan",
                            lambda: codex_client.create_plan(
                                task,
                                user_context=_format_user_context(user_qna),
                                cwd=workspace.path,
                            ),
                        )
                        state_manager.update_state("plan", plan)
                        state_manager.update_state("stage", "plan_ready")

                        if plan.get("status") != "NEEDS_USER_INPUT":
                            break

                        questions = plan.get("questions", [])
                        if not isinstance(questions, list) or not questions:
                            raise RuntimeError("Codex returned NEEDS_USER_INPUT without questions.")

                        new_qna = _prompt_user_for_answers(
                            [str(q) for q in questions],
                            state_manager=state_manager,
                            ui_active=ui is not None and ui.is_running(),
                            poll_interval_sec=user_input_poll_interval_sec,
                            timeout_sec=user_input_timeout_sec,
                        )
                        user_qna.extend(new_qna)
                        state_manager.update_state("user_qna", user_qna)
                else:
                    print("Codex is refining the plan based on feedback...")
                    while True:
                        if skip_plan and isinstance(plan, dict) and plan.get("status") != "NEEDS_USER_INPUT":
                            print("Resuming from existing plan.")
                            state_manager.update_state("plan", plan)
                            state_manager.update_state("stage", "plan_ready")
                            break
                        state_manager.update_state("stage", "refine_plan")
                        plan = _with_codex_status(
                            "refine_plan",
                            lambda: codex_client.refine_plan(
                                state_manager.get_state("plan"),
                                state_manager.get_state("review")
                                or {"status": "REJECTED", "feedback": state_manager.get_state("feedback") or ""},
                                user_context=_format_user_context(user_qna),
                                cwd=workspace.path,
                            ),
                        )
                        state_manager.update_state("plan", plan)
                        state_manager.update_state("stage", "plan_ready")

                        if plan.get("status") != "NEEDS_USER_INPUT":
                            break

                        questions = plan.get("questions", [])
                        if not isinstance(questions, list) or not questions:
                            raise RuntimeError("Codex returned NEEDS_USER_INPUT without questions.")

                        new_qna = _prompt_user_for_answers(
                            [str(q) for q in questions],
                            state_manager=state_manager,
                            ui_active=ui is not None and ui.is_running(),
                            poll_interval_sec=user_input_poll_interval_sec,
                            timeout_sec=user_input_timeout_sec,
                        )
                        user_qna.extend(new_qna)
                        state_manager.update_state("user_qna", user_qna)

                print("Plan created/refined.")

                # 2. Implementation
                print("Claude Code is implementing the plan...")
                if skip_implement and isinstance(implementation_result, str) and implementation_result:
                    print("Resuming from existing implementation output.")
                    implementation_output = {
                        "result": implementation_result,
                        "session_id": claude_session_id,
                        "structured_output": state_manager.get_state("claude_structured_output"),
                    }
                else:
                    state_manager.update_state("stage", "implementing")
                    implementation_output = _with_claude_status(
                        "implement",
                        lambda: claude_code_client.implement(
                            plan,
                            session_id=claude_session_id,
                            cwd=workspace.path,
                            json_schema=CLAUDE_STRUCTURED_SCHEMA,
                            append_system_prompt=CLAUDE_APPEND_SYSTEM_PROMPT,
                        ),
                    )

                if not implementation_output:
                    print("Claude Code implementation failed. Aborting.")
                    break

                claude_session_id = implementation_output.get("session_id")
                claude_step = _get_claude_structured(implementation_output)
                implementation_result = implementation_output.get("result", "")
                state_manager.update_state("implementation_result", implementation_result)
                state_manager.update_state("claude_session_id", claude_session_id)
                state_manager.update_state("claude_structured_output", claude_step)
                state_manager.update_state("stage", "implementation_ready")

                # 2.25 Executor -> Reviewer: if the executor needs clarification, ask a reviewer (Codex).
                question_round = 0
                while claude_step.get("status") in ("NEEDS_REVIEWER", "NEEDS_CODEX") and (
                    max_claude_question_rounds is None or question_round < max_claude_question_rounds
                ):
                    question_round += 1
                    questions = claude_step.get("questions", [])
                    if not isinstance(questions, list) or not questions:
                        raise RuntimeError(
                            "Executor requested reviewer input but did not provide questions."
                        )

                    print(f"Claude has questions (round {question_round}); asking reviewer...")
                    state_manager.add_to_history(
                        f"Executor asked reviewer questions (round {question_round})."
                    )

                    # Reviewer answers the executor; if the reviewer needs more info, ask the user.
                    while True:
                        reviewer_answer = _with_codex_status(
                            "answer_executor",
                            lambda: codex_client.answer_executor(
                                questions=[str(q) for q in questions],
                                context={"task": task, "plan": plan},
                                user_context=_format_user_context(user_qna),
                                cwd=workspace.path,
                            ),
                        )
                        state_manager.update_state("reviewer_answer_to_executor", reviewer_answer)

                        if reviewer_answer.get("status") != "NEEDS_USER_INPUT":
                            break

                        user_questions = reviewer_answer.get("questions", [])
                        if not isinstance(user_questions, list) or not user_questions:
                            raise RuntimeError(
                                "Reviewer returned NEEDS_USER_INPUT without questions."
                            )

                        new_qna = _prompt_user_for_answers(
                            [str(q) for q in user_questions],
                            state_manager=state_manager,
                            ui_active=ui is not None and ui.is_running(),
                            poll_interval_sec=user_input_poll_interval_sec,
                            timeout_sec=user_input_timeout_sec,
                        )
                        user_qna.extend(new_qna)
                        state_manager.update_state("user_qna", user_qna)

                    if reviewer_answer.get("status") != "ANSWER":
                        raise RuntimeError("Reviewer did not return an ANSWER for the executor.")
                    answer_text = str(reviewer_answer.get("answer", "")).strip()
                    if not answer_text:
                        raise RuntimeError("Reviewer returned an empty answer.")

                    followup = (
                        "Continue implementing the plan.\n\n"
                        "Here are answers from the reviewer to your questions:\n"
                        f"{answer_text}\n"
                    )
                    implementation_output = _with_claude_status(
                        "implement_followup",
                        lambda: claude_code_client.implement(
                            followup,
                            session_id=claude_session_id,
                            cwd=workspace.path,
                            json_schema=CLAUDE_STRUCTURED_SCHEMA,
                            append_system_prompt=CLAUDE_APPEND_SYSTEM_PROMPT,
                        ),
                    )
                    if not implementation_output:
                        raise RuntimeError("Claude Code follow-up failed after Codex answered questions.")

                    claude_session_id = implementation_output.get("session_id") or claude_session_id
                    claude_step = _get_claude_structured(implementation_output)
                    implementation_result = implementation_output.get("result", "")
                    state_manager.update_state("implementation_result", implementation_result)
                    state_manager.update_state("claude_session_id", claude_session_id)
                    state_manager.update_state("claude_structured_output", claude_step)
                    state_manager.update_state("stage", "implementation_ready")

                if claude_step.get("status") == "FAILED":
                    print("Claude reported FAILED; aborting.")
                    state_manager.add_to_history("Claude reported FAILED.")
                    break

                if claude_step.get("status") in ("NEEDS_REVIEWER", "NEEDS_CODEX"):
                    print("Claude still needs reviewer input after max question rounds; aborting.")
                    state_manager.add_to_history(
                        "Claude still needed reviewer input after max question rounds."
                    )
                    break

                print("Implementation attempt complete.")

                # 2.5. Automated tests
                print("Running automated tests...")
                if skip_tests and isinstance(test_results, dict):
                    print("Resuming from existing test results.")
                else:
                    state_manager.update_state("stage", "testing")
                    plan_test_commands = plan.get("test_commands") if isinstance(plan, dict) else None
                    test_results = run_tests(cwd=workspace.path, config=config, test_commands=plan_test_commands)
                    state_manager.update_state("test_results", test_results)
                    state_manager.update_state("stage", "tests_ready")

                # 3. Review
                print("Codex is reviewing the implementation...")
                diff = workspace.get_diff()
                while True:
                    if skip_review and isinstance(review, dict):
                        print("Resuming from existing review.")
                        state_manager.update_state("review", review)
                        state_manager.update_state("stage", "review_ready")
                        break
                    state_manager.update_state("stage", "reviewing")
                    review = _with_codex_status(
                        "review",
                        lambda: codex_client.review(
                            plan,
                            implementation_result,
                            diff=diff,
                            test_results=test_results,
                            user_context=_format_user_context(user_qna),
                            cwd=workspace.path,
                        ),
                    )
                    state_manager.update_state("review", review)
                    state_manager.update_state("stage", "review_ready")

                    if review.get("status") != "NEEDS_USER_INPUT":
                        break

                    questions = review.get("questions", [])
                    if not isinstance(questions, list) or not questions:
                        raise RuntimeError("Codex returned NEEDS_USER_INPUT without questions.")

                    new_qna = _prompt_user_for_answers(
                        [str(q) for q in questions],
                        state_manager=state_manager,
                        ui_active=ui is not None and ui.is_running(),
                        poll_interval_sec=user_input_poll_interval_sec,
                        timeout_sec=user_input_timeout_sec,
                    )
                    user_qna.extend(new_qna)
                    state_manager.update_state("user_qna", user_qna)

                if review.get("status") == "APPROVED":
                    approved = True
                    state_manager.update_state("approved", True)
                    print("Implementation APPROVED.")
                    state_manager.add_to_history("Implementation approved.")
                else:
                    feedback = review.get("feedback", "No feedback provided.")
                    print(f"Implementation REJECTED. Feedback: {feedback}")
                    state_manager.add_to_history(f"Implementation rejected. Feedback: {feedback}")
                    state_manager.update_state("feedback", feedback)
                    state_manager.update_state("approved", False)

                resume_used = True

        if not approved:
            print("Max iterations reached. Task failed.")
            state_manager.add_to_history("Max iterations reached. Task failed.")
            state_manager.update_state("stage", "failed")
        else:
            # Persist changes (git commit or copy-back) when approved.
            try:
                if persisted:
                    state_manager.add_to_history("Persistence already completed; skipping.")
                elif workspace.strategy == "worktree" and commit_on_approval:
                    commit_message = commit_message_template.format(task=task, run_id=state_manager.run_id)
                    commit_sha = workspace.commit_changes(commit_message)
                    state_manager.update_state("commit_sha", commit_sha)
                    state_manager.update_state("branch_name", workspace.branch_name)
                    if workspace.branch_name:
                        print(f"Committed to branch: {workspace.branch_name}")
                    if commit_sha:
                        print(f"Commit: {commit_sha}")
                    if auto_merge_on_approval:
                        merge_branch = workspace.branch_name
                        merge_message = merge_commit_message_template.format(
                            task=task or "",
                            run_id=state_manager.run_id,
                            branch=merge_branch or "",
                            target=merge_target_branch,
                        )
                        state_manager.update_state("merge_branch", merge_branch)
                        state_manager.update_state("merge_target_branch", merge_target_branch)
                        state_manager.update_state("stage", "merging")
                        state_manager.update_state("merge_status", "running")
                        merge_result = _auto_merge_worktree_branch(
                            repo_path=repo_path,
                            branch_name=merge_branch,
                            target_branch=merge_target_branch,
                            merge_style=merge_style,
                            dirty_main_policy=dirty_main_policy,
                            dirty_main_commit_message_template=dirty_main_commit_message_template,
                            merge_commit_message=merge_message,
                            claude_client=claude_code_client,
                            task=task,
                            run_id=state_manager.run_id,
                            plan=plan if isinstance(plan, dict) else None,
                            reviewer_decisions={"reviewer-1": review} if isinstance(review, dict) else None,
                            candidate=None,
                            note_fn=lambda msg: (print(msg), state_manager.add_to_history(msg)),
                        )
                        state_manager.update_state(
                            "merge_status", "merged" if merge_result.get("merged") else "failed"
                        )
                        state_manager.update_state(
                            "merge_commit_sha", merge_result.get("merge_commit_sha")
                        )
                        state_manager.update_state(
                            "dirty_main_commit_sha", merge_result.get("dirty_main_commit_sha")
                        )
                        if merge_result.get("conflict_files"):
                            state_manager.update_state(
                                "merge_conflict_files", merge_result.get("conflict_files")
                            )
                        if merge_result.get("claude_merge_summary"):
                            state_manager.update_state(
                                "merge_resolution_summary",
                                merge_result.get("claude_merge_summary"),
                            )
                        if merge_result.get("merged"):
                            persisted = True
                            if delete_worktree_on_merge:
                                force_cleanup_worktree = True
                            if delete_branch_on_merge and merge_branch:
                                merge_branch_to_delete = merge_branch
                        else:
                            persisted = False
                            err = merge_result.get("error") or "unknown merge error"
                            state_manager.add_to_history(f"Auto-merge failed: {err}")
                            state_manager.update_state("merge_error", err)
                            print(f"Auto-merge failed: {err}")
                    else:
                        persisted = True
                elif workspace.strategy == "copy" and apply_changes_on_success:
                    workspace.apply_to_repo()
                    persisted = True
                    print(f"Applied changes to repo: {repo_path}")
                else:
                    persisted = True
            except Exception as e:
                persisted = False
                state_manager.add_to_history(f"Persistence step failed: {e}")
                print(f"Persistence step failed: {e}")
            state_manager.update_state("persisted", persisted)
            state_manager.update_state("stage", "complete" if persisted else "persistence_failed")

        # Reviewer handoff summary (single-agent path)
        if not multi_agent_enabled:
            try:
                diff = workspace.get_diff()
                candidate = {
                    "id": "single",
                    "reviewer_id": "reviewer-1",
                    "executor_id": "executor-1",
                    "status": "APPROVED" if approved else "REJECTED",
                    "test_summary": _summarize_test_results(test_results or {}),
                    "executor_summary": implementation_result,
                    "diff_preview": "\n".join(diff.splitlines()[:40]) if diff else "",
                }
                candidates_text = _candidate_summary_text(candidate)
                handoff_prompt = _review_candidates_prompt(
                    task=task or "",
                    candidates_text=candidates_text,
                    user_context=_format_user_context(user_qna),
                    final_handoff=True,
                )
                handoff = codex_client.run_structured(
                    prompt=handoff_prompt,
                    schema_path=_reviewer_decision_schema_path(),
                    cwd=workspace.path,
                )
                state_manager.update_state("handoff", {"reviewer-1": handoff})
                if telegram_client:
                    _send_telegram_message(
                        state_manager=state_manager,
                        telegram=telegram_client,
                        text=(
                            f"Reviewer summary:\n{handoff.get('summary')}\n\n"
                            f"Next:\n{handoff.get('next_prompt')}"
                        ),
                        label="handoff_summary:single",
                    )
            except Exception as e:
                state_manager.add_to_history(f"Handoff summary failed: {e}")

        run_completed = True
    finally:
        workspace_to_cleanup = cleanup_workspace or workspace
        should_cleanup = run_completed and (
            cleanup_policy == "always"
            or (cleanup_policy == "on_success" and approved and persisted)
            or force_cleanup_worktree
        )
        if should_cleanup:
            if workspace_to_cleanup:
                workspace_to_cleanup.cleanup()
        else:
            if workspace_to_cleanup:
                print(f"Workspace retained at: {workspace_to_cleanup.path}")
        if run_completed and delete_branch_on_merge and merge_branch_to_delete:
            _delete_local_branch(
                repo_path,
                merge_branch_to_delete,
                note_fn=lambda msg: (print(msg), state_manager.add_to_history(msg)),
            )
        if ui and ui.is_running() and not ui_keep_alive_after_run and not session_mode:
            ui.stop()
        if run_completed:
            state_manager.update_state("run_status", "stopped")
            state_manager.update_state("run_completed", True)

    print("--- Orchestration Complete ---")
    print(f"Logs and state saved to: {state_manager.log_dir}")

if __name__ == "__main__":
    main()
