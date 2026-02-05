import subprocess
import unittest
from unittest import mock

import test_runner


class TestRunnerTimeoutTest(unittest.TestCase):
    def test_run_command_timeout_returns_result(self) -> None:
        cmd = ["npm", "run", "lint"]
        timeout_sec = 1
        stderr = "lint still running"
        stdout = "partial output"

        with mock.patch("test_runner.subprocess.run") as mocked_run:
            mocked_run.side_effect = subprocess.TimeoutExpired(
                cmd, timeout_sec, output=stdout, stderr=stderr
            )
            result = test_runner.run_command(cmd, cwd=".", timeout_sec=timeout_sec)

        self.assertEqual(result.exit_code, 124)
        self.assertIn("timed out", result.stderr.lower())
        self.assertIn(stderr, result.stderr)
        self.assertEqual(result.stdout, stdout)

    def test_null_command_timeout_falls_back_to_default(self) -> None:
        config = {"testing": {"timeout_sec": 12, "install_if_missing": False}}
        test_commands = [
            {"id": "unit", "kind": "unit", "command": ["echo", "ok"], "timeout_sec": None}
        ]
        cmd_result = test_runner.CommandResult(
            command=["echo", "ok"], exit_code=0, stdout="ok", stderr="", duration_ms=1
        )

        with mock.patch("test_runner.run_command", return_value=cmd_result) as mocked_run:
            test_runner.run_tests(cwd=".", config=config, test_commands=test_commands)

        mocked_run.assert_called_once()
        _, kwargs = mocked_run.call_args
        self.assertEqual(kwargs.get("timeout_sec"), 12)


if __name__ == "__main__":
    unittest.main()
