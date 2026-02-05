import copy
import json
import os
import sys
import time
import re
import html
import inspect
from datetime import datetime
from typing import Any, Dict, Optional

import streamlit as st
try:
    import yaml  # type: ignore
except ModuleNotFoundError:
    yaml = None

_ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT_DIR not in sys.path:
    sys.path.insert(0, _ROOT_DIR)

from log_parser import extract_claude_events, extract_codex_events, merge_events

def _compute_status_message(state: Dict[str, Any]) -> str:
    awaiting_admin = state.get("awaiting_admin_decision")
    if isinstance(awaiting_admin, dict) and awaiting_admin.get("request_id"):
        return "Awaiting admin decision (choose an option below)."

    awaiting_input = state.get("awaiting_user_input")
    if isinstance(awaiting_input, dict) and awaiting_input.get("request_id"):
        return "Awaiting user input (answer the questions below)."

    awaiting_task = state.get("awaiting_initial_task")
    if isinstance(awaiting_task, dict) and awaiting_task.get("request_id"):
        return "Awaiting initial task to start."

    agent_runtime = state.get("agent_runtime")
    running_infos: list[dict] = []
    if isinstance(agent_runtime, dict):
        for agent_id, info in agent_runtime.items():
            if not isinstance(info, dict):
                continue
            if str(info.get("status") or "") != "Running":
                continue
            role = str(info.get("role") or "").strip() or "agent"
            phase = str(info.get("phase") or "").strip()
            running_infos.append({"id": agent_id, "role": role, "phase": phase})

    def _format_waiting(label: str, role: str) -> str:
        names = [info["id"] for info in running_infos if info["role"] == role]
        if not names:
            return ""
        if len(names) <= 3:
            return f"{label} waiting for {', '.join(names)}."
        return f"{label} waiting for {len(names)} {role}s."

    if running_infos:
        phases = {info["phase"] for info in running_infos}
        # Prefer more specific explanations over generic "running" text.
        if any(phase.startswith("execute:") for phase in phases):
            return _format_waiting("Executing candidates:", "executor") or "Executing candidates: waiting for executors."
        if any(phase.startswith("review_candidates") for phase in phases):
            return _format_waiting("Reviewing candidates:", "reviewer") or "Reviewing candidates: waiting for reviewers."
        if "handoff" in phases:
            return _format_waiting("Final handoff summaries:", "reviewer") or "Final handoff summaries in progress."
        if any(phase.startswith("plan") for phase in phases):
            return _format_waiting("Planning in progress:", "reviewer") or "Planning in progress."

    stage = str(state.get("stage") or "").strip()
    candidates = state.get("candidates") if isinstance(state.get("candidates"), dict) else {}
    if not isinstance(candidates, dict):
        candidates = {}

    def _candidate_counts() -> tuple[int, int, int]:
        running = done = failed = 0
        for cand in candidates.values():
            if not isinstance(cand, dict):
                continue
            status = str(cand.get("status") or "")
            if status == "RUNNING":
                running += 1
            elif status == "DONE":
                done += 1
            elif status == "FAILED":
                failed += 1
        return running, done, failed

    running, done, failed = _candidate_counts()
    if running > 0:
        return f"Executing candidates: {running} running, {done} done, {failed} failed."

    review_errors = state.get("review_errors")
    if isinstance(review_errors, dict) and review_errors:
        return "Reviewer decision errors detected. Check History/Raw state."

    approved = state.get("approved") is True
    persisted = state.get("persisted") is True

    if stage == "planning":
        return "Planning: reviewers are drafting plans. Next: create candidates."
    if stage == "plan_ready":
        return "Plans ready. Next: create candidate workspaces and execute."
    if stage == "executing":
        return f"Executing candidates: {running} running, {done} done, {failed} failed."
    if stage == "tests_ready":
        return "Candidates finished. Next: reviewers evaluate candidates."
    if stage == "reviewing":
        return "Reviewing candidates: reviewers are choosing a winner."
    if stage == "review_ready":
        return "Reviews ready: computing consensus or awaiting admin decision."
    if stage == "complete":
        if approved and persisted:
            return "Approved and persisted. Next: run will stop/idle."
        if approved and not persisted:
            return "Approved, but persistence is incomplete. Check History for details."
        return "Stage marked complete, but approval status is unclear."
    if stage == "persistence_failed":
        return "Approved, but persistence failed. Check History for details."
    if stage == "failed":
        return "Run failed (max iterations or unrecoverable error)."
    if stage == "idle":
        return "Idle."
    run_status = str(state.get("run_status") or "").strip()
    if run_status:
        return f"Run status: {run_status}."
    return ""

def _read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        # File may be mid-write; retry on next refresh.
        return None


def _read_text(path: str, *, max_chars: int = 100_000) -> str:
    try:
        with open(path, "r") as f:
            data = f.read()
        if len(data) > max_chars:
            return data[-max_chars:]
        return data
    except FileNotFoundError:
        return ""

def _read_yaml(path: str) -> Optional[Dict[str, Any]]:
    if yaml is None:
        return None
    try:
        with open(path, "r") as f:
            payload = yaml.safe_load(f)
        return payload if isinstance(payload, dict) else None
    except FileNotFoundError:
        return None
    except Exception:
        return None


def _read_config(path: str) -> Optional[Dict[str, Any]]:
    lower = path.lower()
    if lower.endswith(".json"):
        return _read_json(path)
    if lower.endswith(".yaml") or lower.endswith(".yml"):
        return _read_yaml(path)
    return None


def _resolve_repo_config_path(repo_path: str) -> Optional[str]:
    if not repo_path:
        return None
    candidates = [
        os.path.join(repo_path, ".luigi", "config.json"),
        os.path.join(repo_path, ".luigi", "config.yaml"),
        os.path.join(repo_path, ".luigi", "config.yml"),
        os.path.join(repo_path, "luigi.config.json"),
        os.path.join(repo_path, "luigi.config.yaml"),
        os.path.join(repo_path, "luigi.config.yml"),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return None


def _load_base_config(repo_path: str) -> Dict[str, Any]:
    config_path = _resolve_repo_config_path(repo_path)
    if config_path:
        payload = _read_config(config_path)
        if isinstance(payload, dict):
            return payload

    default_yaml = os.path.join(_ROOT_DIR, "config.yaml")
    default_json = os.path.join(_ROOT_DIR, "config.json")
    if os.path.isfile(default_yaml):
        payload = _read_yaml(default_yaml)
        if isinstance(payload, dict):
            return payload
    if os.path.isfile(default_json):
        payload = _read_json(default_json)
        if isinstance(payload, dict):
            return payload
    return {}


def _write_yaml(path: str, payload: Dict[str, Any]) -> None:
    if yaml is None:
        raise RuntimeError("PyYAML is required to write YAML config files.")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        yaml.safe_dump(payload, f, sort_keys=False, default_flow_style=False)

def _read_tail_lines(path: str, *, max_lines: int = 2000, max_chars: int = 200_000) -> list[str]:
    data = _read_text(path, max_chars=max_chars)
    if not data:
        return []
    lines = data.splitlines()
    if max_lines and len(lines) > max_lines:
        lines = lines[-max_lines:]
    return lines


def _container_supports_height() -> bool:
    try:
        return "height" in inspect.signature(st.container).parameters
    except (TypeError, ValueError):
        return False

def _log_stats(path: str) -> dict:
    try:
        stat = os.stat(path)
        return {
            "size": stat.st_size,
            "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
        }
    except OSError:
        return {}

def _render_log_panel(
    *,
    title: str,
    path: Optional[str],
    max_lines: int,
    max_chars: int,
    wrap: bool,
    newest_first: bool,
    full_max_chars: int,
) -> None:
    st.subheader(title)
    if not path:
        st.info("Log path not available.")
        return
    stats = _log_stats(path)
    if stats:
        st.caption(f"Path: {path} • Size: {stats.get('size')} bytes • Updated: {stats.get('mtime')}")
    else:
        st.caption(f"Path: {path}")

    tail_lines = _read_tail_lines(path, max_lines=max_lines, max_chars=max_chars)
    if newest_first:
        tail_lines = list(reversed(tail_lines))
    tail_text = "\n".join(tail_lines)
    if not tail_text:
        st.code("(no log output yet)")
    else:
        direction = "newest first" if newest_first else "chronological"
        st.caption(f"Showing last {max_lines} lines ({direction}).")
        if wrap:
            st.text_area("Latest output", tail_text, height=420)
        else:
            st.code(tail_text)

    st.download_button(
        "Download latest output",
        tail_text or "",
        file_name=f"{os.path.basename(path)}.tail.txt",
    )

    with st.expander("Older log history (collapsed)", expanded=False):
        st.caption("Older logs are not loaded by default.")
        load_key = f"load_full_{title}"
        if st.button("Load older logs", key=load_key):
            st.session_state[load_key] = True
        if st.session_state.get(load_key):
            full_text = _read_text(path, max_chars=full_max_chars)
            if not full_text:
                st.info("No log content available.")
            else:
                full_lines = full_text.splitlines()
                if max_lines and len(full_lines) > max_lines:
                    older_lines = full_lines[:-max_lines]
                else:
                    older_lines = []
                if not older_lines:
                    st.info("No older logs (only recent output is available).")
                else:
                    if stats and stats.get("size", 0) > full_max_chars:
                        st.caption("Older logs are truncated to the configured max chars.")
                    older_text = "\n".join(older_lines)
                    if wrap:
                        st.text_area("Older output", older_text, height=280)
                    else:
                        st.code(older_text)
            st.download_button(
                "Download full log",
                full_text,
                file_name=os.path.basename(path),
            )


def _render_unified_log(
    *,
    codex_path: Optional[str],
    claude_path: Optional[str],
    max_lines: int,
    max_chars: int,
    wrap: bool,
    newest_first: bool,
    max_events: int,
) -> None:
    st.subheader("Unified activity feed")
    st.markdown(
        """
<style>
.luigi-activity-item {
  margin: 0 0 6px 0;
  padding: 4px 6px;
  border-radius: 6px;
  border: 1px solid rgba(127, 127, 127, 0.20);
}
.luigi-activity-meta {
  font-size: 0.78rem;
  opacity: 0.75;
  line-height: 1.05;
  margin: 0;
  padding: 0;
}
.luigi-activity-text {
  margin: 2px 0 0 0;
  padding: 0;
  line-height: 1.25;
}
</style>
""",
        unsafe_allow_html=True,
    )
    lookback = int(st.session_state.get("activity_lookback", 1))
    eff_lines = max(100, int(max_lines) * lookback)
    eff_chars = max(10_000, int(max_chars) * lookback)

    col_a, col_b, col_c = st.columns([1, 1, 2])
    with col_a:
        if st.button("Load older", key="activity_load_older"):
            st.session_state["activity_lookback"] = min(lookback + 1, 20)
            st.rerun()
    with col_b:
        if st.button("Reset", key="activity_reset"):
            st.session_state["activity_lookback"] = 1
            st.rerun()
    with col_c:
        st.caption(f"Parsing last ~{eff_lines} lines / {eff_chars} chars from each log.")

    codex_lines = _read_tail_lines(codex_path, max_lines=eff_lines, max_chars=eff_chars) if codex_path else []
    claude_lines = _read_tail_lines(claude_path, max_lines=eff_lines, max_chars=eff_chars) if claude_path else []

    events = merge_events(extract_codex_events(codex_lines), extract_claude_events(claude_lines))
    if not events:
        st.info("No agent activity yet.")
        return

    if newest_first:
        events = list(reversed(events))
    if max_events and len(events) > max_events:
        events = events[:max_events]

    for ev in events:
        source = ev.source.title()
        ts = ev.timestamp.strftime("%Y-%m-%d %H:%M:%S") if ev.timestamp else ""
        meta = f"{source} • {ts}" if ts else source
        meta_html = html.escape(meta)
        text_html = html.escape(ev.text or "")
        st.markdown(
            f"<div class='luigi-activity-item'>"
            f"<div class='luigi-activity-meta'>{meta_html}</div>"
            f"<div class='luigi-activity-text'>{text_html}</div>"
            f"</div>",
            unsafe_allow_html=True,
        )
        if ev.details:
            with st.expander("Details", expanded=False):
                st.code(ev.details)

def _atomic_write_json(path: str, payload: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w") as f:
        json.dump(payload, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)

def _write_user_answers(log_dir: str, request_id: str, answers: list[dict]) -> None:
    out_path = os.path.join(log_dir, f"user_input_response_{request_id}.json")
    payload = {"request_id": request_id, "answers": answers}
    _atomic_write_json(out_path, payload)

def _write_initial_task(log_dir: str, request_id: str, task: str) -> None:
    out_path = os.path.join(log_dir, f"initial_task_response_{request_id}.json")
    payload = {"request_id": request_id, "task": task}
    _atomic_write_json(out_path, payload)


def _write_admin_decision(log_dir: str, request_id: str, choice: int, notes: str) -> None:
    out_path = os.path.join(log_dir, f"admin_decision_response_{request_id}.json")
    payload = {"request_id": request_id, "choice": choice, "notes": notes}
    _atomic_write_json(out_path, payload)

def _parse_allowed_user_ids(raw: str) -> tuple[list[int], list[str]]:
    tokens = re.split(r"[,\s]+", (raw or "").strip())
    ids: list[int] = []
    invalid: list[str] = []
    for token in tokens:
        if not token:
            continue
        if re.fullmatch(r"\d+", token):
            ids.append(int(token))
        else:
            invalid.append(token)
    # Deduplicate while preserving order.
    seen = set()
    unique_ids: list[int] = []
    for value in ids:
        if value not in seen:
            seen.add(value)
            unique_ids.append(value)
    return unique_ids, invalid


def _render_config_panel(*, repo_path: str) -> None:
    st.subheader("Config")
    st.caption("Edits apply on the next run. Current run is unchanged.")

    if not repo_path:
        st.info("Repo path is unknown; cannot write repo-local config.")
        return
    if yaml is None:
        st.error("PyYAML is required to edit config in the UI. Install with: pip install -r requirements.txt")
        return

    target_path = os.path.join(repo_path, ".luigi", "config.yaml")
    base_path = _resolve_repo_config_path(repo_path)
    base_label = base_path or os.path.join(_ROOT_DIR, "config.yaml")
    st.caption(f"Base config: `{base_label}`")
    st.caption(f"Will write: `{target_path}`")

    base_config = _load_base_config(repo_path)
    if not isinstance(base_config, dict) or not base_config:
        st.warning("Could not load a base config; a minimal config will be created.")
        base_config = {}

    agents_cfg = base_config.get("agents") if isinstance(base_config, dict) else {}
    if not isinstance(agents_cfg, dict):
        agents_cfg = {}

    reviewers_existing = agents_cfg.get("reviewers") if isinstance(agents_cfg, dict) else None
    executors_existing = agents_cfg.get("executors") if isinstance(agents_cfg, dict) else None
    if not isinstance(reviewers_existing, list):
        reviewers_existing = [{"id": "reviewer-1", "kind": "codex"}]
    if not isinstance(executors_existing, list):
        executors_existing = [{"id": "executor-1", "kind": "claude"}]

    assignment_cfg = agents_cfg.get("assignment") if isinstance(agents_cfg, dict) else None
    if not isinstance(assignment_cfg, dict):
        assignment_cfg = {}
    assignment_mode = assignment_cfg.get("mode") or "round_robin"
    executors_per_plan_default = assignment_cfg.get("executors_per_plan")
    try:
        executors_per_plan_default = int(executors_per_plan_default)
    except Exception:
        executors_per_plan_default = 1

    telegram_cfg = base_config.get("telegram") if isinstance(base_config, dict) else {}
    if not isinstance(telegram_cfg, dict):
        telegram_cfg = {}

    with st.form("config_form"):
        st.markdown("### Agents")
        reviewer_count = st.number_input(
            "Reviewers",
            min_value=1,
            max_value=10,
            value=max(1, len(reviewers_existing)),
            step=1,
        )
        executor_count = st.number_input(
            "Executors",
            min_value=1,
            max_value=10,
            value=max(1, len(executors_existing)),
            step=1,
        )

        max_per_plan = min(2, int(executor_count))
        if max_per_plan < 1:
            max_per_plan = 1
        if max_per_plan == 1:
            st.selectbox(
                "Executors per plan (max 2)",
                options=[1],
                index=0,
                disabled=True,
                help="Increase executors to allow 2 per plan.",
            )
            executors_per_plan = 1
        else:
            executors_per_plan = st.slider(
                "Executors per plan (max 2)",
                min_value=1,
                max_value=max_per_plan,
                value=min(max_per_plan, max(1, int(executors_per_plan_default))),
            )

        reviewers: list[dict] = []
        for idx in range(int(reviewer_count)):
            default_kind = "codex"
            if idx < len(reviewers_existing):
                default_kind = str(reviewers_existing[idx].get("kind") or "codex").lower()
            kind = st.selectbox(
                f"Reviewer {idx + 1} kind",
                options=["codex", "claude"],
                index=0 if default_kind != "claude" else 1,
                key=f"reviewer_kind_{idx}",
            )
            reviewers.append({"id": f"reviewer-{idx + 1}", "kind": kind})

        total_slots = int(reviewer_count) * int(executors_per_plan)
        assignment_labels: list[str] = []
        for idx in range(int(executor_count)):
            slots: list[tuple[int, int]] = []
            for slot_index in range(total_slots):
                if int(executor_count) == 0:
                    break
                if (slot_index % int(executor_count)) == idx:
                    reviewer_idx = slot_index // int(executors_per_plan)
                    slot_idx = slot_index % int(executors_per_plan)
                    slots.append((reviewer_idx + 1, slot_idx + 1))
            if not slots:
                assignment_labels.append(f"Executor {idx + 1} (unused)")
            elif len(slots) == 1:
                reviewer_idx, slot_idx = slots[0]
                assignment_labels.append(f"Reviewer {reviewer_idx} executor {slot_idx}")
            else:
                slot_text = ", ".join(f"R{r}E{s}" for r, s in slots)
                assignment_labels.append(f"Executor {idx + 1} (used by {slot_text})")

        executors: list[dict] = []
        for idx in range(int(executor_count)):
            default_kind = "claude"
            if idx < len(executors_existing):
                default_kind = str(executors_existing[idx].get("kind") or "claude").lower()
            kind = st.selectbox(
                assignment_labels[idx],
                options=["claude", "codex"],
                index=0 if default_kind != "codex" else 1,
                key=f"executor_kind_{idx}",
            )
            executors.append({"id": f"executor-{idx + 1}", "kind": kind})

        st.markdown("### Telegram")
        st.caption(
            "Tip: concurrent Luigi runs should not share a Telegram bot token. "
            "For safety, set allowed user IDs; if empty, anyone in the chat can respond."
        )
        telegram_enabled = st.checkbox("Enable Telegram", value=bool(telegram_cfg.get("enabled")))
        bot_token = st.text_input(
            "Bot token",
            value=str(telegram_cfg.get("bot_token") or ""),
            type="password",
        )
        chat_id = st.text_input(
            "Chat ID",
            value=str(telegram_cfg.get("chat_id") or ""),
        )
        allowed_ids_default = telegram_cfg.get("allowed_user_ids") if isinstance(telegram_cfg, dict) else []
        if not isinstance(allowed_ids_default, list):
            allowed_ids_default = []
        allowed_ids_text = ", ".join(str(x) for x in allowed_ids_default)
        allowed_ids_raw = st.text_input(
            "Allowed user IDs (comma or space-separated)",
            value=allowed_ids_text,
        )
        poll_interval_sec = st.number_input(
            "Poll interval (sec)",
            min_value=0.5,
            max_value=30.0,
            value=float(telegram_cfg.get("poll_interval_sec") or 2.0),
            step=0.5,
        )

        saved = st.form_submit_button("Save agents + Telegram config")

    if not saved:
        return

    allowed_user_ids, invalid_ids = _parse_allowed_user_ids(allowed_ids_raw)
    if invalid_ids:
        st.warning(f"Ignoring invalid user IDs: {', '.join(invalid_ids)}")
    if telegram_enabled and not allowed_user_ids:
        st.warning("Telegram is enabled with no allowed user IDs. Anyone in the chat can respond.")

    new_config = copy.deepcopy(base_config)
    if not isinstance(new_config, dict):
        new_config = {}

    agents_out: dict = dict(new_config.get("agents") or {})
    agents_out["reviewers"] = reviewers
    agents_out["executors"] = executors
    assignment_out = dict(agents_out.get("assignment") or {})
    assignment_out["mode"] = assignment_mode
    assignment_out["executors_per_plan"] = int(executors_per_plan)
    agents_out["assignment"] = assignment_out
    new_config["agents"] = agents_out

    telegram_out: dict = dict(new_config.get("telegram") or {})
    telegram_out["enabled"] = bool(telegram_enabled)
    telegram_out["bot_token"] = str(bot_token or "").strip()
    telegram_out["chat_id"] = str(chat_id or "").strip()
    telegram_out["allowed_user_ids"] = allowed_user_ids
    telegram_out["poll_interval_sec"] = float(poll_interval_sec)
    new_config["telegram"] = telegram_out

    try:
        _write_yaml(target_path, new_config)
        st.success("Saved. The new config will apply on the next run.")
    except Exception as exc:
        st.error(f"Failed to write config: {exc}")


def main() -> None:
    log_dir = os.environ.get("LUIGI_LOG_DIR", "")
    run_id = os.environ.get("LUIGI_RUN_ID", "")
    project_id = os.environ.get("LUIGI_PROJECT_ID", "")
    repo_path = os.environ.get("LUIGI_REPO_PATH", "")

    st.set_page_config(page_title=f"Luigi • {project_id or run_id or 'run'}", layout="wide")

    st.sidebar.header("Luigi")
    st.sidebar.write(f"**Project**: `{project_id}`" if project_id else "**Project**: (unknown)")
    st.sidebar.write(f"**Run ID**: `{run_id}`" if run_id else "**Run ID**: (unknown)")
    st.sidebar.write(f"**Repo**: `{repo_path}`" if repo_path else "**Repo**: (unknown)")
    st.sidebar.write(f"**Logs**: `{log_dir}`" if log_dir else "**Logs**: (unknown)")

    auto_refresh = st.sidebar.checkbox("Auto-refresh", value=True)
    refresh_sec = st.sidebar.slider("Refresh interval (sec)", min_value=0.5, max_value=5.0, value=1.0, step=0.5)
    st.sidebar.subheader("Logs")
    log_lines = st.sidebar.number_input(
        "Lines to show",
        min_value=100,
        max_value=50000,
        value=500,
        step=100,
    )
    log_wrap = st.sidebar.checkbox("Wrap log lines", value=True)
    log_newest_first = st.sidebar.checkbox("Newest first", value=True)
    log_max_chars = st.sidebar.number_input(
        "Max tail chars",
        min_value=10_000,
        max_value=500_000,
        value=200_000,
        step=10_000,
    )
    log_entry_lines = st.sidebar.number_input(
        "Max activity events",
        min_value=10,
        max_value=500,
        value=80,
        step=10,
    )
    log_full_chars = st.sidebar.number_input(
        "Max full log chars",
        min_value=50_000,
        max_value=2_000_000,
        value=500_000,
        step=50_000,
    )

    if not log_dir:
        st.error("Missing LUIGI_LOG_DIR environment variable.")
        return

    state_path = os.path.join(log_dir, "state.json")
    history_path = os.path.join(log_dir, "history.log")

    state = _read_json(state_path) or {}

    col1, col2 = st.columns([1, 1])
    with col1:
        st.subheader("Status")
        workspace_path = state.get("workspace_path")
        workspace_strategy = state.get("workspace_strategy")
        st.write(f"**Workspace**: `{workspace_path}` ({workspace_strategy})" if workspace_path else "**Workspace**: (unknown)")

        run_status = str(state.get("run_status") or "").strip()
        stage = str(state.get("stage") or "").strip()
        if run_status:
            st.write(f"**Run status**: `{run_status}`")
        if stage:
            st.write(f"**Stage**: `{stage}`")
        status_message = _compute_status_message(state)
        if status_message:
            st.info(status_message)

        review = state.get("review") or {}
        plan = state.get("plan") or {}
        claude_structured = state.get("claude_structured_output") or {}
        codex_status = state.get("codex_status") or "Stopped"
        codex_phase = state.get("codex_phase") or "idle"
        claude_status = state.get("claude_status") or "Stopped"
        claude_phase = state.get("claude_phase") or "idle"

        iteration = state.get("iteration")
        if iteration is None and isinstance(review, dict):
            iteration = review.get("iteration")
        if iteration is not None and str(iteration) != "":
            st.write(f"**Iteration**: {iteration}")
        if isinstance(review, dict) and "status" in review:
            st.write(f"**Review**: `{review.get('status')}`")

        st.write(
            f"**Codex CLI**: `{codex_status}`"
            + (f" ({codex_phase})" if codex_phase and codex_phase != "idle" else "")
        )
        st.write(
            f"**Claude CLI**: `{claude_status}`"
            + (f" ({claude_phase})" if claude_phase and claude_phase != "idle" else "")
        )
        agent_runtime = state.get("agent_runtime")
        if isinstance(agent_runtime, dict) and agent_runtime:
            st.markdown("**Agents**")
            for agent_id in sorted(agent_runtime.keys()):
                info = agent_runtime.get(agent_id)
                if not isinstance(info, dict):
                    continue
                kind = str(info.get("kind") or "")
                role = str(info.get("role") or "")
                status = str(info.get("status") or "")
                phase = str(info.get("phase") or "")
                line = f"- `{agent_id}` ({role}/{kind}): `{status}`"
                if phase and phase != "idle":
                    line += f" ({phase})"
                st.markdown(line)
        if isinstance(claude_structured, dict) and claude_structured.get("status"):
            st.write(f"**Executor result**: `{claude_structured.get('status')}`")

        if isinstance(plan, dict) and plan.get("tasks"):
            st.write(f"**Plan tasks**: {len(plan.get('tasks', []))}")

    with col2:
        st.subheader("History")
        history_text = _read_text(history_path, max_chars=50_000)
        history_body = history_text or "(no history yet)"
        if _container_supports_height():
            with st.container(height=600):
                st.code(history_body)
        else:
            st.code(history_body)

    awaiting = state.get("awaiting_user_input")
    if isinstance(awaiting, dict) and awaiting.get("request_id") and awaiting.get("questions"):
        st.subheader("User input required")
        request_id = str(awaiting.get("request_id"))
        questions = awaiting.get("questions", [])
        if not isinstance(questions, list):
            questions = []

        answers: list[dict] = []
        for idx, q in enumerate(questions):
            q_str = str(q)
            st.markdown(f"**Q{idx + 1}:** {q_str}")
            a = st.text_input(f"Answer {idx + 1}", key=f"answer_{request_id}_{idx}")
            answers.append({"question": q_str, "answer": a})

        if st.button("Submit answers", type="primary"):
            _write_user_answers(log_dir, request_id, answers)
            st.success("Submitted. Luigi should continue shortly.")

    awaiting_task = state.get("awaiting_initial_task")
    if isinstance(awaiting_task, dict) and awaiting_task.get("request_id"):
        st.subheader("Start new plan")
        request_id = str(awaiting_task.get("request_id"))
        task_text = st.text_area("Task prompt", height=120, key=f"task_{request_id}")
        if st.button("Start", type="primary"):
            _write_initial_task(log_dir, request_id, task_text.strip())
            st.success("Task submitted. Luigi should start shortly.")

    awaiting_admin = state.get("awaiting_admin_decision")
    if isinstance(awaiting_admin, dict) and awaiting_admin.get("request_id"):
        st.subheader("Admin decision required")
        request_id = str(awaiting_admin.get("request_id"))
        options = awaiting_admin.get("options", [])
        if not isinstance(options, list):
            options = []
        labels = [str(opt.get("label", f"Option {idx+1}")) for idx, opt in enumerate(options)]
        if labels:
            choice = st.radio("Choose an option", options=list(range(1, len(labels) + 1)), format_func=lambda i: labels[i - 1])
            notes = st.text_area("Notes (optional)")
            if st.button("Submit decision", type="primary"):
                _write_admin_decision(log_dir, request_id, int(choice), notes.strip())
                st.success("Decision submitted.")

    st.divider()

    tabs = st.tabs(
        [
            "Plans",
            "Candidates",
            "Reviews",
            "Handoff",
            "Unified log",
            "Raw logs",
            "Config",
            "Raw state",
        ]
    )

    with tabs[0]:
        st.subheader("Reviewer plans")
        plans_payload = state.get("plans")
        if not plans_payload and isinstance(plan, dict):
            plans_payload = plan
        st.json(plans_payload or {})

    with tabs[1]:
        st.subheader("Executor candidates")
        candidates = state.get("candidates") or {}
        st.json(candidates)

    with tabs[2]:
        st.subheader("Reviewer decisions")
        st.json(state.get("reviews") or review if isinstance(review, dict) else {})

    with tabs[3]:
        st.subheader("End-of-run handoff")
        st.json(state.get("handoff") or {})

    with tabs[4]:
        _render_unified_log(
            codex_path=state.get("codex_log_path"),
            claude_path=state.get("claude_log_path"),
            max_lines=int(log_lines),
            max_chars=int(log_max_chars),
            wrap=bool(log_wrap),
            newest_first=bool(log_newest_first),
            max_events=int(log_entry_lines),
        )

    with tabs[5]:
        _render_log_panel(
            title="Reviewer CLI log",
            path=state.get("codex_log_path"),
            max_lines=int(log_lines),
            max_chars=int(log_max_chars),
            wrap=bool(log_wrap),
            newest_first=bool(log_newest_first),
            full_max_chars=int(log_full_chars),
        )
        _render_log_panel(
            title="Executor CLI log",
            path=state.get("claude_log_path"),
            max_lines=int(log_lines),
            max_chars=int(log_max_chars),
            wrap=bool(log_wrap),
            newest_first=bool(log_newest_first),
            full_max_chars=int(log_full_chars),
        )

    with tabs[6]:
        _render_config_panel(repo_path=repo_path)

    with tabs[7]:
        st.subheader("Raw `state.json`")
        st.json(state)

    if auto_refresh:
        time.sleep(refresh_sec)
        st.rerun()


if __name__ == "__main__":
    main()

