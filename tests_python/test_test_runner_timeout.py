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


if __name__ == "__main__":
    unittest.main()
