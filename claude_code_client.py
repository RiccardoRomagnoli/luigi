
import json
import os
import subprocess
import threading
import time
from datetime import datetime
from typing import Any, Dict, Optional, Union

DEBUG_LOG_PATH = "/Users/ricrom/Code/luigi/.cursor/debug.log"

def _now_ms() -> int:
    return int(time.time() * 1000)

def _debug_run_id(config: Dict[str, Any]) -> Optional[str]:
    log_dir = config.get("log_dir")
    if isinstance(log_dir, str) and log_dir:
        return os.path.basename(os.path.normpath(log_dir))
    return None

def _debug_log(payload: Dict[str, Any]) -> None:
    try:
        with open(DEBUG_LOG_PATH, "a") as handle:
            handle.write(json.dumps(payload) + "\n")
    except OSError:
        pass


def _run_print_stream_json(
    *,
    command: list[str],
    cwd: Optional[str],
    log_f,
    heartbeat_sec: float = 5.0,
) -> tuple[int, Optional[dict], Optional[dict], int]:
    """Run Claude CLI in print mode and stream output line-by-line.

    The Claude CLI supports `--output-format stream-json`, which emits NDJSON events.
    We log all lines as they arrive and parse JSON objects per-line.

    Returns:
      (returncode, last_json_obj, result_event_obj, total_output_chars)
    """
    total_chars = 0
    last_json: Optional[dict] = None
    result_json: Optional[dict] = None

    stop_event = threading.Event()
    activity_event = threading.Event()
    write_lock = threading.Lock()
    start_monotonic = time.monotonic()

    def _write(text: str) -> None:
        if not log_f:
            return
        with write_lock:
            try:
                log_f.write(text)
                log_f.flush()
            except OSError:
                pass

    def _heartbeat() -> None:
        if not log_f or not heartbeat_sec or heartbeat_sec <= 0:
            return
        while not stop_event.is_set():
            # Wait for output activity; if none, emit a heartbeat.
            had_activity = activity_event.wait(timeout=heartbeat_sec)
            if stop_event.is_set():
                return
            if had_activity:
                activity_event.clear()
                continue
            elapsed = int(time.monotonic() - start_monotonic)
            _write(f"\n--- Claude still running ({elapsed}s) ---\n")

    hb_thread: Optional[threading.Thread] = None
    if log_f and heartbeat_sec and heartbeat_sec > 0:
        hb_thread = threading.Thread(target=_heartbeat, daemon=True)
        hb_thread.start()

    proc = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    try:
        if proc.stdout:
            for line in proc.stdout:
                total_chars += len(line)
                activity_event.set()
                _write(line)

                candidate = line.strip()
                if not candidate:
                    continue
                try:
                    obj = json.loads(candidate)
                except Exception:
                    continue
                if isinstance(obj, dict):
                    last_json = obj
                    if obj.get("type") == "result":
                        result_json = obj
    finally:
        returncode = proc.wait()
        stop_event.set()
        if hb_thread:
            hb_thread.join(timeout=1)

    return returncode, last_json, result_json, total_chars

class ClaudeCodeClient:
    """A client to interact with the Claude Code CLI for implementation."""

    def __init__(self, config):
        """Initializes the ClaudeCodeClient."""
        self.config = config
        raw_command = config.get("command", "claude")
        self.command = raw_command if isinstance(raw_command, list) else [raw_command]
        self.log_path = None
        log_dir = config.get("log_dir")
        if isinstance(log_dir, str) and log_dir:
            self.log_path = os.path.join(log_dir, "claude.log")

    def implement(
        self,
        plan: Union[str, Dict[str, Any]],
        session_id: Optional[str] = None,
        cwd: Optional[str] = None,
        json_schema: Optional[Dict[str, Any]] = None,
        append_system_prompt: Optional[str] = None,
    ):
        """Implements the given plan using the Claude Code CLI.

        Args:
            plan: Either a raw prompt string or a structured plan dict containing `claude_prompt`.
            session_id: Optional Claude session ID to resume.
            cwd: Working directory where Claude should operate (target repo/workspace).
            json_schema: Optional JSON schema for structured output (goes into `structured_output`).
            append_system_prompt: Optional system prompt instructions appended to Claude Code defaults.
        """
        prompt = plan
        if isinstance(plan, dict):
            prompt = plan.get("claude_prompt") or json.dumps(plan, indent=2)

        command = [
            *self.command,
            "-p",
            prompt,
            "--model", self.config["model"],
            "--output-format", "stream-json",
            "--include-partial-messages",
        ]

        allowed_tools = self.config.get("allowed_tools")
        if allowed_tools:
            command.extend(["--allowedTools", ",".join(allowed_tools)])

        if append_system_prompt:
            command.extend(["--append-system-prompt", append_system_prompt])

        if json_schema:
            # Claude CLI accepts a JSON schema string; structured output is returned under `structured_output`.
            command.extend(["--json-schema", json.dumps(json_schema)])

        if session_id:
            command.extend(["--resume", session_id])

        if self.config.get("max_turns"):
            command.extend(["--max-turns", str(self.config["max_turns"])])

        log_f = None
        if self.log_path:
            try:
                os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
                log_f = open(self.log_path, "a")
                log_f.write(f"\n=== {datetime.utcnow().isoformat()} Claude implement ===\n")
                log_f.flush()
            except OSError:
                log_f = None

        result = None
        stdout_len = 0
        start_ts = _now_ms()
        # region agent log
        _debug_log(
            {
                "id": f"claude_impl_start_{start_ts}",
                "timestamp": start_ts,
                "location": "claude_code_client.py:implement:start",
                "message": "claude_implement_start",
                "data": {
                    "cwd": cwd,
                    "prompt_len": len(prompt) if isinstance(prompt, str) else 0,
                    "session_id_present": bool(session_id),
                    "has_json_schema": bool(json_schema),
                    "has_append_system_prompt": bool(append_system_prompt),
                    "allowed_tools_count": len(allowed_tools) if isinstance(allowed_tools, list) else 0,
                    "max_turns": self.config.get("max_turns"),
                    "command_bin": self.command[0] if self.command else None,
                    "output_format": "stream-json",
                    "include_partial_messages": True,
                },
                "runId": _debug_run_id(self.config),
                "hypothesisId": "H1",
            }
        )
        # endregion
        try:
            if log_f:
                log_f.write("\n--- Claude stdout/stderr (stream-json) ---\n")
                log_f.flush()
            returncode, last_json, result_json, stdout_len = _run_print_stream_json(
                command=command,
                cwd=cwd,
                log_f=log_f,
            )
            payload = result_json or last_json
            result = subprocess.CompletedProcess(
                args=command,
                returncode=returncode,
                stdout=json.dumps(payload) if isinstance(payload, dict) else "",
                stderr=None,
            )
        except OSError as exc:
            if log_f:
                try:
                    log_f.write(f"\n=== Claude spawn error: {exc} ===\n")
                    log_f.flush()
                except OSError:
                    pass
            print(f"Error running Claude Code CLI: {exc}")
            return None
        finally:
            if log_f:
                try:
                    if result is not None:
                        log_f.write(f"\n=== Claude exit {result.returncode} ===\n")
                    log_f.flush()
                except OSError:
                    pass
                try:
                    log_f.close()
                except OSError:
                    pass

        # region agent log
        end_ts = _now_ms()
        _debug_log(
            {
                "id": f"claude_impl_end_{end_ts}",
                "timestamp": end_ts,
                "location": "claude_code_client.py:implement:end",
                "message": "claude_implement_end",
                "data": {
                    "returncode": result.returncode if result is not None else None,
                    "duration_ms": end_ts - start_ts,
                    "stdout_len": stdout_len,
                    "stderr_logged": bool(log_f),
                },
                "runId": _debug_run_id(self.config),
                "hypothesisId": "H4",
            }
        )
        # endregion

        if result.returncode != 0:
            print(f"Error running Claude Code CLI (exit {result.returncode}).")
            if not log_f:
                print(f"Stderr: {result.stderr}")
            return None

        try:
            # With stream-json, we already parsed line-by-line and stored the last JSON object
            # in `result.stdout` (as a JSON string) for back-compat with existing callers.
            output = json.loads(result.stdout.strip() or "{}")
            return output if isinstance(output, dict) else None
        except json.JSONDecodeError:
            print("Error parsing Claude Code JSON output.")
            print(f"Stdout: {result.stdout}")
            if not log_f:
                print(f"Stderr: {result.stderr}")
            return None

    def run_structured(
        self,
        *,
        prompt: str,
        json_schema: Dict[str, Any],
        cwd: Optional[str] = None,
        session_id: Optional[str] = None,
        allowed_tools_override: Optional[list[str]] = None,
        max_turns_override: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        command = [
            *self.command,
            "-p",
            prompt,
            "--model", self.config.get("model", "opus"),
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--json-schema", json.dumps(json_schema),
        ]

        allowed_tools = (
            allowed_tools_override
            if allowed_tools_override is not None
            else self.config.get("allowed_tools")
        )
        if allowed_tools:
            command.extend(["--allowedTools", ",".join(allowed_tools)])

        if session_id:
            command.extend(["--resume", session_id])

        max_turns = (
            max_turns_override
            if max_turns_override is not None
            else self.config.get("max_turns")
        )
        if max_turns:
            command.extend(["--max-turns", str(max_turns)])

        log_f = None
        if self.log_path:
            try:
                os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
                log_f = open(self.log_path, "a")
                log_f.write(f"\n=== {datetime.utcnow().isoformat()} Claude review ===\n")
                log_f.flush()
            except OSError:
                log_f = None

        result = None
        stdout_len = 0
        start_ts = _now_ms()
        # region agent log
        _debug_log(
            {
                "id": f"claude_struct_start_{start_ts}",
                "timestamp": start_ts,
                "location": "claude_code_client.py:run_structured:start",
                "message": "claude_structured_start",
                "data": {
                    "cwd": cwd,
                    "prompt_len": len(prompt) if isinstance(prompt, str) else 0,
                    "session_id_present": bool(session_id),
                    "allowed_tools_count": len(allowed_tools) if isinstance(allowed_tools, list) else 0,
                    "max_turns": max_turns,
                    "command_bin": self.command[0] if self.command else None,
                    "output_format": "stream-json",
                    "include_partial_messages": True,
                },
                "runId": _debug_run_id(self.config),
                "hypothesisId": "H2",
            }
        )
        # endregion
        try:
            if log_f:
                log_f.write("\n--- Claude stdout/stderr (stream-json) ---\n")
                log_f.flush()
            returncode, last_json, result_json, stdout_len = _run_print_stream_json(
                command=command,
                cwd=cwd,
                log_f=log_f,
            )
            payload = result_json or last_json
            result = subprocess.CompletedProcess(
                args=command,
                returncode=returncode,
                stdout=json.dumps(payload) if isinstance(payload, dict) else "",
                stderr=None,
            )
        except OSError as exc:
            if log_f:
                try:
                    log_f.write(f"\n=== Claude spawn error: {exc} ===\n")
                    log_f.flush()
                except OSError:
                    pass
            return None
        finally:
            if log_f:
                try:
                    if result is not None:
                        log_f.write(f"\n=== Claude exit {result.returncode} ===\n")
                    log_f.flush()
                except OSError:
                    pass
                try:
                    log_f.close()
                except OSError:
                    pass

        # region agent log
        end_ts = _now_ms()
        _debug_log(
            {
                "id": f"claude_struct_end_{end_ts}",
                "timestamp": end_ts,
                "location": "claude_code_client.py:run_structured:end",
                "message": "claude_structured_end",
                "data": {
                    "returncode": result.returncode if result is not None else None,
                    "duration_ms": end_ts - start_ts,
                    "stdout_len": stdout_len,
                    "stderr_logged": bool(log_f),
                },
                "runId": _debug_run_id(self.config),
                "hypothesisId": "H2",
            }
        )
        # endregion

        if not result or result.returncode != 0:
            return None

        try:
            payload = json.loads(result.stdout.strip() or "{}")
        except json.JSONDecodeError:
            return None

        # Claude CLI returns a JSON "envelope" and puts schema-constrained output under
        # `structured_output`. Return the structured object so callers receive the schema shape.
        if isinstance(payload, dict):
            structured = payload.get("structured_output")
            if isinstance(structured, dict):
                return structured

            # Back-compat: some mocks/older outputs may return the structured object at top-level.
            if "status" in payload and "type" not in payload:
                return payload

        return None
