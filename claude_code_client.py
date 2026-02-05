
import json
import os
import subprocess
from datetime import datetime
from typing import Any, Dict, Optional, Union

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
            "--output-format", "json",
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
        try:
            if log_f:
                result = subprocess.run(command, cwd=cwd, stdout=subprocess.PIPE, stderr=log_f, text=True)
            else:
                result = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
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
                        log_f.write("\n--- Claude stdout ---\n")
                        log_f.write(result.stdout or "")
                        log_f.write(f"\n=== Claude exit {result.returncode} ===\n")
                    log_f.flush()
                except OSError:
                    pass
                try:
                    log_f.close()
                except OSError:
                    pass

        if result.returncode != 0:
            print(f"Error running Claude Code CLI (exit {result.returncode}).")
            if not log_f:
                print(f"Stderr: {result.stderr}")
            return None

        try:
            output = json.loads(result.stdout.strip() or "{}")
            return output
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
            "--output-format", "json",
            "--json-schema", json.dumps(json_schema),
        ]

        allowed_tools = allowed_tools_override if allowed_tools_override is not None else self.config.get("allowed_tools")
        if allowed_tools:
            command.extend(["--allowedTools", ",".join(allowed_tools)])

        if session_id:
            command.extend(["--resume", session_id])

        max_turns = max_turns_override if max_turns_override is not None else self.config.get("max_turns")
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
        try:
            if log_f:
                result = subprocess.run(command, cwd=cwd, stdout=subprocess.PIPE, stderr=log_f, text=True)
            else:
                result = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
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
                        log_f.write("\n--- Claude stdout ---\n")
                        log_f.write(result.stdout or "")
                        log_f.write(f"\n=== Claude exit {result.returncode} ===\n")
                    log_f.flush()
                except OSError:
                    pass
                try:
                    log_f.close()
                except OSError:
                    pass

        if not result or result.returncode != 0:
            return None

        try:
            return json.loads(result.stdout.strip() or "{}")
        except json.JSONDecodeError:
            return None
