import os
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


def _truncate(text: str, *, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... [truncated] ..."


def _coerce_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


@dataclass
class CommandResult:
    command: List[str]
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int

    def to_dict(self, *, max_output_chars: int = 8000) -> Dict:
        return {
            "command": self.command,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "stdout": _truncate(self.stdout, limit=max_output_chars),
            "stderr": _truncate(self.stderr, limit=max_output_chars),
        }


def run_command(command: List[str], *, cwd: str, timeout_sec: Optional[int] = None) -> CommandResult:
    start = time.time()
    try:
        proc = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        duration_ms = int((time.time() - start) * 1000)
        return CommandResult(
            command=command,
            exit_code=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            duration_ms=duration_ms,
        )
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.time() - start) * 1000)
        stdout = _coerce_text(getattr(exc, "stdout", None) or getattr(exc, "output", None))
        stderr = _coerce_text(getattr(exc, "stderr", None))
        timeout_label = (
            f"Timed out after {timeout_sec} seconds."
            if timeout_sec is not None
            else "Timed out."
        )
        if stderr:
            stderr = f"{timeout_label}\n{stderr}"
        else:
            stderr = timeout_label
        return CommandResult(
            command=command,
            exit_code=124,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
        )


def run_tests(*, cwd: str, config: Dict, test_commands: Optional[List[Dict[str, Any]]] = None) -> Dict:
    """Run test commands, returning a structured results dict.

    The config format is:
      testing:
        install_command: ["npm", "install"]
        unit_command: ["npm", "test"]          # fallback only
        e2e_command: ["npx", "playwright", "test"]  # fallback only
        timeout_sec: 1800
    """
    testing_cfg = config.get("testing", {})
    timeout_sec = testing_cfg.get("timeout_sec")
    if timeout_sec is not None:
        if not isinstance(timeout_sec, (int, float)) or isinstance(timeout_sec, bool) or timeout_sec <= 0:
            raise ValueError("testing.timeout_sec must be a positive number or null.")
    install_if_missing = testing_cfg.get("install_if_missing", False)

    install_command = testing_cfg.get("install_command", ["npm", "install"])
    fallback_unit_command = testing_cfg.get("unit_command", ["npm", "test"])
    fallback_e2e_command = testing_cfg.get("e2e_command", ["npx", "playwright", "test"])

    results: Dict = {
        "cwd": os.path.abspath(cwd),
        "installed_deps": None,
        "commands": [],
    }

    # Optional best-effort dependency install for Node projects if node_modules is missing.
    if (
        install_if_missing
        and os.path.exists(os.path.join(cwd, "package.json"))
        and not os.path.exists(os.path.join(cwd, "node_modules"))
    ):
        install_res = run_command(install_command, cwd=cwd, timeout_sec=timeout_sec)
        results["installed_deps"] = install_res.to_dict()

        # If deps install fails, skip tests (they'll almost certainly fail too).
        if install_res.exit_code != 0:
            return results

    # Prefer test commands produced by Codex plan (prompt-driven).
    # Only fall back to config when the plan did not provide any `test_commands` field.
    if test_commands is not None:
        commands_to_run = test_commands
    else:
        commands_to_run = [
            {"id": "unit", "kind": "unit", "command": fallback_unit_command},
            {"id": "e2e", "kind": "e2e", "command": fallback_e2e_command},
        ]

    for idx, cmd_spec in enumerate(commands_to_run):
        command = cmd_spec.get("command")
        if not isinstance(command, list) or not command:
            continue
        cmd_timeout = cmd_spec.get("timeout_sec")
        if cmd_timeout is None:
            cmd_timeout = timeout_sec
        elif (
            not isinstance(cmd_timeout, (int, float))
            or isinstance(cmd_timeout, bool)
            or cmd_timeout <= 0
        ):
            raise ValueError(
                f"test_commands[{idx}].timeout_sec must be a positive number or null."
            )
        res = run_command(command, cwd=cwd, timeout_sec=cmd_timeout)
        results["commands"].append(
            {
                "id": cmd_spec.get("id"),
                "kind": cmd_spec.get("kind"),
                "label": cmd_spec.get("label"),
                "result": res.to_dict(),
            }
        )

    return results

