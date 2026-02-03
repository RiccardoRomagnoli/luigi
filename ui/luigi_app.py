import json
import os
import sys
import time
import re
from datetime import datetime
from typing import Any, Dict, Optional

import streamlit as st

_ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT_DIR not in sys.path:
    sys.path.insert(0, _ROOT_DIR)

from log_parser import extract_claude_events, extract_codex_events, merge_events

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

def _read_tail_lines(path: str, *, max_lines: int = 2000, max_chars: int = 200_000) -> list[str]:
    data = _read_text(path, max_chars=max_chars)
    if not data:
        return []
    lines = data.splitlines()
    if max_lines and len(lines) > max_lines:
        lines = lines[-max_lines:]
    return lines

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
        st.markdown(
            f"<div class='luigi-activity-item'>"
            f"<div class='luigi-activity-meta'>{meta}</div>"
            f"<div class='luigi-activity-text'>{ev.text}</div>"
            f"</div>",
            unsafe_allow_html=True,
        )
        if ev.details:
            with st.expander("Details", expanded=False):
                st.code(ev.details)

def _write_user_answers(log_dir: str, request_id: str, answers: list[dict]) -> None:
    out_path = os.path.join(log_dir, f"user_input_response_{request_id}.json")
    payload = {"request_id": request_id, "answers": answers}
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)

def _write_initial_task(log_dir: str, request_id: str, task: str) -> None:
    out_path = os.path.join(log_dir, f"initial_task_response_{request_id}.json")
    payload = {"request_id": request_id, "task": task}
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)


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

        review = state.get("review") or {}
        plan = state.get("plan") or {}
        claude_structured = state.get("claude_structured_output") or {}
        codex_status = state.get("codex_status") or "Stopped"
        codex_phase = state.get("codex_phase") or "idle"
        claude_status = state.get("claude_status") or "Stopped"
        claude_phase = state.get("claude_phase") or "idle"

        st.write(f"**Iteration**: {review.get('iteration', '')}" if isinstance(review, dict) else "")
        if isinstance(review, dict) and "status" in review:
            st.write(f"**Review**: `{review.get('status')}`")

        st.write(
            f"**Codex**: `{codex_status}`"
            + (f" ({codex_phase})" if codex_phase and codex_phase != "idle" else "")
        )
        st.write(
            f"**Claude**: `{claude_status}`"
            + (f" ({claude_phase})" if claude_phase and claude_phase != "idle" else "")
        )
        if isinstance(claude_structured, dict) and claude_structured.get("status"):
            st.write(f"**Claude result**: `{claude_structured.get('status')}`")

        if isinstance(plan, dict) and plan.get("tasks"):
            st.write(f"**Plan tasks**: {len(plan.get('tasks', []))}")

    with col2:
        st.subheader("History")
        history_text = _read_text(history_path, max_chars=50_000)
        st.code(history_text or "(no history yet)")

    awaiting = state.get("awaiting_user_input")
    if isinstance(awaiting, dict) and awaiting.get("request_id") and awaiting.get("questions"):
        st.subheader("User input required (Codex)")
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
        st.subheader("Start Luigi run")
        request_id = str(awaiting_task.get("request_id"))
        task_text = st.text_area("Task prompt", height=120, key=f"task_{request_id}")
        if st.button("Start", type="primary"):
            _write_initial_task(log_dir, request_id, task_text.strip())
            st.success("Task submitted. Luigi should start shortly.")

    st.divider()

    tabs = st.tabs(
        [
            "Plan",
            "Claude",
            "Codex→Claude answer",
            "Tests",
            "Review",
            "Unified log",
            "Codex log",
            "Claude log",
            "Raw state",
        ]
    )

    with tabs[0]:
        st.subheader("Plan (Codex)")
        st.json(plan if isinstance(plan, dict) else {})

    with tabs[1]:
        st.subheader("Claude output")
        st.json(
            {
                "structured_output": state.get("claude_structured_output"),
                "result": state.get("implementation_result"),
                "session_id": state.get("claude_session_id"),
            }
        )

    with tabs[2]:
        st.subheader("Codex answer to Claude")
        st.json(state.get("codex_answer_to_claude") or {})

    with tabs[3]:
        st.subheader("Test results")
        st.json(state.get("test_results") or {})

    with tabs[4]:
        st.subheader("Review (Codex)")
        st.json(review if isinstance(review, dict) else {})

    with tabs[5]:
        _render_unified_log(
            codex_path=state.get("codex_log_path"),
            claude_path=state.get("claude_log_path"),
            max_lines=int(log_lines),
            max_chars=int(log_max_chars),
            wrap=bool(log_wrap),
            newest_first=bool(log_newest_first),
            max_events=int(log_entry_lines),
        )

    with tabs[6]:
        _render_log_panel(
            title="Codex CLI log",
            path=state.get("codex_log_path"),
            max_lines=int(log_lines),
            max_chars=int(log_max_chars),
            wrap=bool(log_wrap),
            newest_first=bool(log_newest_first),
            full_max_chars=int(log_full_chars),
        )

    with tabs[7]:
        _render_log_panel(
            title="Claude CLI log",
            path=state.get("claude_log_path"),
            max_lines=int(log_lines),
            max_chars=int(log_max_chars),
            wrap=bool(log_wrap),
            newest_first=bool(log_newest_first),
            full_max_chars=int(log_full_chars),
        )

    with tabs[8]:
        st.subheader("Raw `state.json`")
        st.json(state)

    if auto_refresh:
        time.sleep(refresh_sec)
        st.rerun()


if __name__ == "__main__":
    main()

