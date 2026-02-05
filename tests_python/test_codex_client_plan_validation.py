import os
import sys
import unittest

from codex_client import CodexClient


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class CodexClientPlanValidationTest(unittest.TestCase):
    def test_invalid_plan_raises(self) -> None:
        mock_path = os.path.join(REPO_ROOT, "tests", "mocks", "codex_mock_invalid_plan.py")
        config = {
            "command": [sys.executable, mock_path],
            "model": "gpt-5.2-codex",
            "reasoning_effort": "xhigh",
            "sandbox": "read-only",
            "approval_policy": "never",
        }
        client = CodexClient(config)

        with self.assertRaises(RuntimeError):
            client.create_plan("Test plan validation", cwd=REPO_ROOT)


if __name__ == "__main__":
    unittest.main()
