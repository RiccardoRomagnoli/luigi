
import argparse
import json
import os
import sys
import time
import uuid
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


def _read_json_file(path: str) -> dict | None:
    try:
        with open(path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        return None


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


def _load_resume_state_by_id(*, logs_root: str, repo_path: str, run_id: str) -> tuple[str, dict]:
    if not run_id:
        raise RuntimeError("Missing run id for resume.")
    run_dir = os.path.join(logs_root, run_id)
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
            with open(response_path, "r") as f:
                payload = json.load(f)
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

    start = time.time()
    while True:
        if os.path.exists(response_path):
            with open(response_path, "r") as f:
                payload = json.load(f)
            try:
                os.remove(response_path)
            except OSError:
                pass

            task_text = str(payload.get("task", "")).strip()
            if not task_text:
                raise RuntimeError("Empty task provided via UI.")
            return _finalize(task_text)

        if timeout_sec is not None and (time.time() - start) > timeout_sec:
            raise RuntimeError("Timed out waiting for initial task.")

        time.sleep(poll_interval_sec)


CLAUDE_STRUCTURED_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status"],
    "properties": {
        "status": {"type": "string", "enum": ["DONE", "NEEDS_CODEX", "FAILED"]},
        "questions": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
}

CLAUDE_APPEND_SYSTEM_PROMPT = (
    "You are running under Luigi orchestration in non-interactive mode.\n"
    "If you need clarification, DO NOT ask the user.\n"
    "Instead, set structured_output.status=\"NEEDS_CODEX\" and populate structured_output.questions.\n"
    "When you have completed the requested work, set structured_output.status=\"DONE\" and provide a short summary.\n"
    "If you cannot proceed, set structured_output.status=\"FAILED\" and explain in the summary.\n"
)


def _get_claude_structured(output: dict) -> dict:
    structured = output.get("structured_output")
    if isinstance(structured, dict):
        return structured
    # Back-compat with older/mocked outputs: treat as DONE.
    return {"status": "DONE", "summary": output.get("result", "")}


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
        # Back-compat (old name)
        os.path.join(repo_path, ".combo-agents", "config.json"),
        os.path.join(repo_path, ".combo-agents", "config.yaml"),
        os.path.join(repo_path, ".combo-agents", "config.yml"),
        os.path.join(repo_path, "combo-agents.config.json"),
        os.path.join(repo_path, "combo-agents.config.yaml"),
        os.path.join(repo_path, "combo-agents.config.yml"),
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate

    return os.path.join(os.path.dirname(__file__), "config.json")


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
    resuming = resume_info is not None
    if resuming:
        state_manager.add_to_history("Resuming previous run.")
    # Persist core run metadata early for monitoring.
    state_manager.update_state("run_id", state_manager.run_id)
    state_manager.update_state("repo_path", repo_path)
    state_manager.update_state("config_path", os.path.abspath(config_path))
    state_manager.update_state("run_status", "running")
    state_manager.update_state("codex_status", "Stopped")
    state_manager.update_state("claude_status", "Stopped")
    state_manager.update_state("codex_phase", "idle")
    state_manager.update_state("claude_phase", "idle")
    state_manager.update_state("codex_log_path", os.path.join(state_manager.log_dir, "codex.log"))
    state_manager.update_state("claude_log_path", os.path.join(state_manager.log_dir, "claude.log"))

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
    max_claude_question_rounds = int(config.get("orchestrator", {}).get("max_claude_question_rounds", 5))

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
        )
    state_manager.update_state("workspace_path", workspace.path)
    state_manager.update_state("workspace_strategy", workspace.strategy)
    if workspace.branch_name:
        state_manager.update_state("workspace_branch", workspace.branch_name)
    print(f"Workspace: {workspace.path} ({workspace.strategy})")

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

    if not task:
        task = _prompt_user_for_initial_task(
            state_manager=state_manager,
            ui_active=ui is not None and ui.is_running(),
            poll_interval_sec=user_input_poll_interval_sec,
            timeout_sec=user_input_timeout_sec,
        )
        state_manager.update_state("task", task)

    resume_used = False
    run_completed = False
    try:
        while not approved and iteration < config["orchestrator"]["max_iterations"]:
            iteration += 1
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

            # 2.25 Claude -> Codex: if Claude needs clarification, route to Codex.
            question_round = 0
            while claude_step.get("status") == "NEEDS_CODEX" and question_round < max_claude_question_rounds:
                question_round += 1
                questions = claude_step.get("questions", [])
                if not isinstance(questions, list) or not questions:
                    raise RuntimeError("Claude requested Codex help but did not provide questions.")

                print(f"Claude has questions (round {question_round}); asking Codex...")
                state_manager.add_to_history(f"Claude asked Codex questions (round {question_round}).")

                # Codex answers Claude; if Codex needs more info, Codex asks the user.
                while True:
                    codex_answer = _with_codex_status(
                        "answer_claude",
                        lambda: codex_client.answer_claude(
                            questions=[str(q) for q in questions],
                            context={"task": task, "plan": plan},
                            user_context=_format_user_context(user_qna),
                            cwd=workspace.path,
                        ),
                    )
                    state_manager.update_state("codex_answer_to_claude", codex_answer)

                    if codex_answer.get("status") != "NEEDS_USER_INPUT":
                        break

                    user_questions = codex_answer.get("questions", [])
                    if not isinstance(user_questions, list) or not user_questions:
                        raise RuntimeError("Codex returned NEEDS_USER_INPUT without questions.")

                    new_qna = _prompt_user_for_answers(
                        [str(q) for q in user_questions],
                        state_manager=state_manager,
                        ui_active=ui is not None and ui.is_running(),
                        poll_interval_sec=user_input_poll_interval_sec,
                        timeout_sec=user_input_timeout_sec,
                    )
                    user_qna.extend(new_qna)
                    state_manager.update_state("user_qna", user_qna)

                if codex_answer.get("status") != "ANSWER":
                    raise RuntimeError("Codex did not return an ANSWER for Claude.")
                answer_text = str(codex_answer.get("answer", "")).strip()
                if not answer_text:
                    raise RuntimeError("Codex returned an empty answer for Claude.")

                followup = (
                    "Continue implementing the plan.\n\n"
                    "Here are answers from Codex to your questions:\n"
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

            if claude_step.get("status") == "NEEDS_CODEX":
                print("Claude still needs Codex after max question rounds; aborting.")
                state_manager.add_to_history("Claude still needed Codex after max question rounds.")
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
                    persisted = True
                    if workspace.branch_name:
                        print(f"Committed to branch: {workspace.branch_name}")
                    if commit_sha:
                        print(f"Commit: {commit_sha}")
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
        run_completed = True
    finally:
        if run_completed and (
            cleanup_policy == "always" or (cleanup_policy == "on_success" and approved and persisted)
        ):
            workspace.cleanup()
        else:
            print(f"Workspace retained at: {workspace.path}")
        if ui and ui.is_running() and not ui_keep_alive_after_run:
            ui.stop()
        if run_completed:
            state_manager.update_state("run_status", "stopped")
            state_manager.update_state("run_completed", True)

    print("--- Orchestration Complete ---")
    print(f"Logs and state saved to: {state_manager.log_dir}")

if __name__ == "__main__":
    main()
