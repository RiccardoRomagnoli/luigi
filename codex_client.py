"""
Codex CLI client for planning and review.

This wraps `codex exec` in non-interactive mode and requests structured JSON
outputs via `--output-schema` + `--output-last-message`.
"""

import json
import os
import subprocess
import tempfile
from datetime import datetime
from typing import Any, Dict, Optional


def _schemas_dir() -> str:
    return os.path.join(os.path.dirname(__file__), "schemas")


def _plan_schema_path() -> str:
    return os.path.join(_schemas_dir(), "codex_plan.schema.json")


def _review_schema_path() -> str:
    return os.path.join(_schemas_dir(), "codex_review.schema.json")

def _answer_schema_path() -> str:
    return os.path.join(_schemas_dir(), "codex_answer.schema.json")


class CodexClient:
    """A client to interact with the Codex CLI for planning and code review."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        raw_command = config.get("command", "codex")
        self.command = raw_command if isinstance(raw_command, list) else [raw_command]
        self.model = config.get("model", "gpt-5.2")
        # "xhigh" here means Codex config key `model_reasoning_effort`.
        self.reasoning_effort = config.get("reasoning_effort", "xhigh")
        self.verbosity = config.get("verbosity")  # optional: low|medium|high
        self.sandbox = config.get("sandbox", "read-only")
        self.approval_policy = config.get("approval_policy", "never")
        self.log_path = None
        log_dir = config.get("log_dir")
        if isinstance(log_dir, str) and log_dir:
            self.log_path = os.path.join(log_dir, "codex.log")

    @staticmethod
    def _is_nonempty_str(value: Any) -> bool:
        return isinstance(value, str) and value.strip() != ""

    @staticmethod
    def _is_nonempty_str_list(value: Any) -> bool:
        if not isinstance(value, list) or not value:
            return False
        return all(isinstance(item, str) and item.strip() != "" for item in value)

    @staticmethod
    def _extract_phase(prompt: str) -> str:
        for line in prompt.splitlines():
            if line.startswith("PHASE:"):
                return line.split(":", 1)[1].strip()
        return "UNKNOWN"

    @staticmethod
    def _read_log_tail(path: str, *, max_chars: int = 8000) -> str:
        try:
            with open(path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                offset = max(size - max_chars, 0)
                handle.seek(offset)
                data = handle.read().decode("utf-8", errors="replace")
            return data
        except OSError:
            return ""

    def _validate_plan(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(plan, dict):
            raise RuntimeError("Codex plan output invalid: expected an object.")

        status = plan.get("status")
        if status is None:
            status = "NEEDS_USER_INPUT" if self._is_nonempty_str_list(plan.get("questions")) else "OK"
            plan["status"] = status

        if status == "NEEDS_USER_INPUT":
            if not self._is_nonempty_str_list(plan.get("questions")):
                raise RuntimeError(
                    "Codex plan output invalid: NEEDS_USER_INPUT requires a non-empty questions list."
                )
            return plan

        if status != "OK":
            raise RuntimeError(f"Codex plan output invalid: unknown status {status!r}.")

        if not self._is_nonempty_str(plan.get("claude_prompt")):
            raise RuntimeError("Codex plan output invalid: claude_prompt must be a non-empty string.")

        tasks = plan.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            raise RuntimeError("Codex plan output invalid: tasks must be a non-empty list.")
        for idx, task in enumerate(tasks):
            if not isinstance(task, dict):
                raise RuntimeError(f"Codex plan output invalid: tasks[{idx}] must be an object.")
            for field in ("id", "title", "description"):
                if not self._is_nonempty_str(task.get(field)):
                    raise RuntimeError(
                        f"Codex plan output invalid: tasks[{idx}].{field} must be a non-empty string."
                    )

        test_commands = plan.get("test_commands")
        if test_commands is None or test_commands == []:
            plan["test_commands"] = None
        else:
            if not isinstance(test_commands, list):
                raise RuntimeError("Codex plan output invalid: test_commands must be a list or null.")
            for idx, cmd_spec in enumerate(test_commands):
                if not isinstance(cmd_spec, dict):
                    raise RuntimeError(f"Codex plan output invalid: test_commands[{idx}] must be an object.")
                if not self._is_nonempty_str(cmd_spec.get("id")):
                    raise RuntimeError(
                        f"Codex plan output invalid: test_commands[{idx}].id must be a non-empty string."
                    )
                command = cmd_spec.get("command")
                if not isinstance(command, list) or not command:
                    raise RuntimeError(
                        f"Codex plan output invalid: test_commands[{idx}].command must be a non-empty list."
                    )
                if not all(isinstance(item, str) and item.strip() != "" for item in command):
                    raise RuntimeError(
                        f"Codex plan output invalid: test_commands[{idx}].command must contain non-empty strings."
                    )

        return plan

    def _validate_review(self, review: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(review, dict):
            raise RuntimeError("Codex review output invalid: expected an object.")

        status = review.get("status")
        if status == "NEEDS_USER_INPUT":
            if not self._is_nonempty_str_list(review.get("questions")):
                raise RuntimeError(
                    "Codex review output invalid: NEEDS_USER_INPUT requires a non-empty questions list."
                )
            return review

        if status not in ("APPROVED", "REJECTED"):
            raise RuntimeError(f"Codex review output invalid: unknown status {status!r}.")

        if not self._is_nonempty_str(review.get("feedback")):
            raise RuntimeError("Codex review output invalid: feedback must be a non-empty string.")

        return review

    def _validate_answer(self, answer: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(answer, dict):
            raise RuntimeError("Codex answer output invalid: expected an object.")

        status = answer.get("status")
        if status == "NEEDS_USER_INPUT":
            if not self._is_nonempty_str_list(answer.get("questions")):
                raise RuntimeError(
                    "Codex answer output invalid: NEEDS_USER_INPUT requires a non-empty questions list."
                )
            return answer

        if status != "ANSWER":
            raise RuntimeError(f"Codex answer output invalid: unknown status {status!r}.")

        if not self._is_nonempty_str(answer.get("answer")):
            raise RuntimeError("Codex answer output invalid: answer must be a non-empty string.")

        return answer

    def create_plan(self, task: str, *, user_context: str = "", cwd: str) -> Dict[str, Any]:
        """Creates an implementation plan for a given task."""
        prompt = self._plan_prompt(task, user_context=user_context)
        plan = self._run_codex_json(prompt=prompt, schema_path=_plan_schema_path(), cwd=cwd)
        return self._validate_plan(plan)

    def run_structured(self, *, prompt: str, schema_path: str, cwd: str) -> Dict[str, Any]:
        return self._run_codex_json(prompt=prompt, schema_path=schema_path, cwd=cwd)

    def refine_plan(
        self,
        plan: Dict[str, Any],
        review: Dict[str, Any],
        *,
        user_context: str = "",
        cwd: str,
    ) -> Dict[str, Any]:
        """Refines an implementation plan based on feedback."""
        prompt = self._refine_plan_prompt(plan=plan, review=review, user_context=user_context)
        plan = self._run_codex_json(prompt=prompt, schema_path=_plan_schema_path(), cwd=cwd)
        return self._validate_plan(plan)

    def review(
        self,
        plan: Dict[str, Any],
        implementation_result: str,
        *,
        diff: str,
        test_results: Optional[Dict[str, Any]] = None,
        user_context: str = "",
        cwd: str,
    ) -> Dict[str, Any]:
        """Reviews the implementation and provides feedback."""
        prompt = self._review_prompt(
            plan=plan,
            implementation_result=implementation_result,
            diff=diff,
            test_results=test_results,
            user_context=user_context,
        )
        review = self._run_codex_json(prompt=prompt, schema_path=_review_schema_path(), cwd=cwd)
        return self._validate_review(review)

    def answer_claude(
        self,
        *,
        questions: list[str],
        context: Dict[str, Any],
        user_context: str = "",
        cwd: str,
    ) -> Dict[str, Any]:
        """Answer Claude's questions. If Codex needs more info, it should ask the user."""
        prompt = self._answer_claude_prompt(questions=questions, context=context, user_context=user_context)
        answer = self._run_codex_json(prompt=prompt, schema_path=_answer_schema_path(), cwd=cwd)
        return self._validate_answer(answer)

    def _run_codex_json(self, *, prompt: str, schema_path: str, cwd: str) -> Dict[str, Any]:
        if not os.path.exists(schema_path):
            raise RuntimeError(f"Missing schema file: {schema_path}")

        cmd = [
            *self.command,
            "exec",
            "--color",
            "never",
            "--skip-git-repo-check",
            "--sandbox",
            self.sandbox,
            "--model",
            self.model,
            "--cd",
            os.path.abspath(cwd),
            "--output-schema",
            os.path.abspath(schema_path),
        ]

        # Config overrides (see Codex config reference).
        if self.approval_policy:
            cmd.extend(["-c", f"approval_policy={self.approval_policy}"])
        if self.reasoning_effort:
            cmd.extend(["-c", f"model_reasoning_effort={self.reasoning_effort}"])
        if self.verbosity:
            cmd.extend(["-c", f"model_verbosity={self.verbosity}"])

        # Capture the final assistant message to a file for reliable parsing.
        with tempfile.NamedTemporaryFile(prefix="codex-last-message-", suffix=".json", delete=False) as tmp:
            out_path = tmp.name
        cmd.extend(["--output-last-message", out_path])
        cmd.append(prompt)

        log_f = None
        if self.log_path:
            try:
                os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
                log_f = open(self.log_path, "a")
                phase = self._extract_phase(prompt)
                log_f.write(f"\n=== {datetime.utcnow().isoformat()} Codex {phase} ===\n")
                log_f.flush()
            except OSError:
                log_f = None

        result = None
        try:
            if log_f:
                result = subprocess.run(cmd, stdout=log_f, stderr=log_f, text=True)
            else:
                result = subprocess.run(cmd, capture_output=True, text=True)
        except OSError as exc:
            if log_f:
                try:
                    log_f.write(f"\n=== Codex spawn error: {exc} ===\n")
                    log_f.flush()
                except OSError:
                    pass
            raise RuntimeError(f"Codex CLI failed to start: {exc}") from exc
        finally:
            if log_f:
                try:
                    if result is not None:
                        log_f.write(f"\n=== Codex exit {result.returncode} ===\n")
                    log_f.flush()
                except OSError:
                    pass
                try:
                    log_f.close()
                except OSError:
                    pass
        try:
            if result.returncode != 0:
                if self.log_path:
                    log_tail = self._read_log_tail(self.log_path)
                    raise RuntimeError(
                        "Codex CLI failed.\n"
                        f"Command: {' '.join(cmd)}\n"
                        f"Log tail:\n{log_tail}\n"
                    )
                raise RuntimeError(
                    "Codex CLI failed.\n"
                    f"Command: {' '.join(cmd)}\n"
                    f"Stdout:\n{result.stdout}\n"
                    f"Stderr:\n{result.stderr}\n"
                )

            with open(out_path, "r") as f:
                content = f.read().strip()
            if not content:
                raise RuntimeError("Codex produced an empty final message.")

            return json.loads(content)
        finally:
            try:
                os.remove(out_path)
            except OSError:
                pass

    @staticmethod
    def _plan_prompt(task: str, *, user_context: str) -> str:
        return (
            "PHASE: PLAN\n"
            "You are Codex acting as the planning/review agent in a two-agent system.\n"
            "You must produce a detailed, tool-friendly plan that Claude Code CLI can execute.\n\n"
            "Constraints:\n"
            "- Keep tasks incremental and testable.\n"
            "- Testing strategy MUST be derived from (a) the user task and (b) what the target project already uses.\n"
            "- If the project already has tests, use its existing commands/frameworks.\n"
            "- If you need to introduce tests into a JS/TS project, default to vitest (unit) and Playwright (E2E)\n"
            "  unless the user explicitly requests something else.\n"
            "- Include `test_commands` for the orchestrator to run after Claude finishes implementing.\n"
            "- Always include ALL top-level fields: status, claude_prompt, tasks, test_commands, questions, notes.\n"
            '- For a normal plan, set status to "OK". For clarification, set status to "NEEDS_USER_INPUT".\n'
            "- Use null or empty arrays for fields that do not apply.\n"
            "- If you require clarification from the user, do NOT guess. Output JSON with:\n"
            '  {"status":"NEEDS_USER_INPUT","questions":["..."]}\n'
            "- Output MUST be valid JSON matching the provided schema.\n\n"
            f"User task:\n{task}\n"
            + (f"\nUser context / answers:\n{user_context}\n" if user_context else "")
        )

    @staticmethod
    def _refine_plan_prompt(*, plan: Dict[str, Any], review: Dict[str, Any], user_context: str) -> str:
        return (
            "PHASE: REFINE_PLAN\n"
            "Update the existing plan based on reviewer feedback.\n"
            "Do not remove tasks unless they are explicitly invalid; append or adjust tasks as needed.\n"
            "- Always include ALL top-level fields: status, claude_prompt, tasks, test_commands, questions, notes.\n"
            '- For a normal plan, set status to "OK". For clarification, set status to "NEEDS_USER_INPUT".\n'
            "- Use null or empty arrays for fields that do not apply.\n"
            "- If you require clarification from the user, do NOT guess. Output JSON with:\n"
            '  {"status":"NEEDS_USER_INPUT","questions":["..."]}\n'
            "Output MUST be valid JSON matching the provided schema.\n\n"
            f"Existing plan JSON:\n{json.dumps(plan, indent=2)}\n\n"
            f"Reviewer JSON:\n{json.dumps(review, indent=2)}\n"
            + (f"\nUser context / answers:\n{user_context}\n" if user_context else "")
        )

    @staticmethod
    def _review_prompt(
        *,
        plan: Dict[str, Any],
        implementation_result: str,
        diff: str,
        test_results: Optional[Dict[str, Any]],
        user_context: str,
    ) -> str:
        return (
            "PHASE: REVIEW\n"
            "You are Codex acting as a strict reviewer.\n"
            "Decide if the implementation satisfies the plan.\n"
            "If rejected, provide concrete feedback and additional tasks to reach completion.\n"
            "If test results show failures, you MUST reject and explain what to fix.\n"
            "- Always include ALL top-level fields: status, feedback, additional_tasks, confidence, questions, notes.\n"
            "- Use null or empty arrays for fields that do not apply.\n"
            "- If you require clarification from the user, do NOT guess. Output JSON with:\n"
            '  {"status":"NEEDS_USER_INPUT","questions":["..."]}\n'
            "Output MUST be valid JSON matching the provided schema.\n\n"
            f"Plan JSON:\n{json.dumps(plan, indent=2)}\n\n"
            f"Claude Code result summary:\n{implementation_result}\n\n"
            f"Test results JSON:\n{json.dumps(test_results or {}, indent=2)}\n\n"
            + (f"User context / answers:\n{user_context}\n\n" if user_context else "")
            + f"Code diff:\n{diff}\n"
        )

    @staticmethod
    def _answer_claude_prompt(*, questions: list[str], context: Dict[str, Any], user_context: str) -> str:
        return (
            "PHASE: ANSWER_CLAUDE\n"
            "You are Codex. Claude (the implementer) is asking questions.\n"
            "Answer concisely and with actionable guidance.\n"
            "- Always include ALL top-level fields: status, answer, questions, notes.\n"
            "- Use null or empty arrays for fields that do not apply.\n"
            "If you need clarification from the user to answer, do NOT guess. Output JSON with:\n"
            '  {"status":"NEEDS_USER_INPUT","questions":["..."]}\n'
            "Otherwise output JSON with:\n"
            '  {"status":"ANSWER","answer":"..."}\n'
            "Output MUST be valid JSON matching the provided schema.\n\n"
            f"Claude questions:\n{json.dumps(questions, indent=2)}\n\n"
            f"Context JSON:\n{json.dumps(context, indent=2)}\n\n"
            + (f"User context / answers:\n{user_context}\n" if user_context else "")
        )
